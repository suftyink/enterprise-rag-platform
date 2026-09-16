from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.api.dependencies import get_current_actor, get_identity_access
from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import IdentityRole
from erp_assistant.infrastructure.database import Base, get_session
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.main import app
from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
)
from erp_assistant.rag.types import DocumentStatus
from erp_assistant.rag.vector_store import VectorHit, get_vector_store
from erp_assistant.workers.dispatcher import get_document_dispatcher
from tests.document_factory import make_docx
from tests.identity_factory import trusted_identity
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
    actor, identity_access = trusted_identity(
        "user-a",
        "tenant-a",
        roles=frozenset({IdentityRole.KNOWLEDGE_ADMIN}),
        include_global=True,
    )

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_vector_store] = lambda: vector_store
    app.dependency_overrides[get_document_dispatcher] = lambda: (
        lambda document_id: f"task-{document_id}"
    )
    app.dependency_overrides[get_current_actor] = lambda: actor
    app.dependency_overrides[get_identity_access] = lambda: identity_access
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
    scope: str = "TENANT",
    document_key: str = "inventory-sop",
    filename: str = "inventory.pdf",
    media_type: str = "application/pdf",
) -> Response:
    return await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={
            "file": (
                filename,
                content
                if content is not None
                else make_pdf(["Inventory stock policy contains enough searchable text. " * 4]),
                media_type,
            )
        },
        data={
            "scope": scope,
            "documentKey": document_key,
        },
    )


@pytest.mark.asyncio
async def test_upload_status_and_hash_deduplication(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, _, storage, _ = knowledge_api
    content = make_pdf(["Inventory stock policy contains enough searchable text. " * 4])
    first = await _upload(client, content=content)
    second = await _upload(client, content=content)
    distinct_identity = await _upload(
        client,
        content=content,
        document_key="shipping-sop",
    )
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["documentId"] == second.json()["documentId"]
    assert distinct_identity.status_code == 202
    assert distinct_identity.json()["duplicate"] is False
    assert distinct_identity.json()["documentId"] != first.json()["documentId"]
    assert len(storage.objects) == 2

    status_response = await client.get(
        f"/api/assistant/v1/knowledge/documents/{first.json()['documentId']}",
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "QUEUED"
    assert status_response.json()["publicationStatus"] == "UNPUBLISHED"
    assert status_response.json()["retentionStatus"] == "RETAINED"
    assert status_response.json()["governanceRevision"] == 0
    assert status_response.json()["retrievalEligible"] is False
    assert status_response.json()["jobStatus"] == "QUEUED"
    assert status_response.json()["attempt"] == 0
    assert status_response.json()["chunkCount"] == 0
    assert status_response.json()["warnings"] == []
    assert status_response.json()["pipelineRevision"] is None
    assert status_response.json()["cleanupRequired"] is False
    hidden = await client.get(
        f"/api/assistant/v1/knowledge/documents/{first.json()['documentId']}",
        params={"tenantId": "tenant-b", "operatorId": "user-b"},
    )
    assert hidden.status_code == 200


@pytest.mark.asyncio
async def test_protected_api_ignores_forged_identity_fields_and_requires_bearer(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, factory, _, _ = knowledge_api
    forged = await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={
            "file": (
                "forged.pdf",
                make_pdf(["Trusted actor identity must win over request data. " * 4]),
                "application/pdf",
            )
        },
        data={
            "tenantId": "tenant-b",
            "operatorId": "attacker",
            "documentKey": "forged-identity",
        },
    )
    assert forged.status_code == 202
    async with factory() as session:
        document = await session.get(KnowledgeDocument, UUID(forged.json()["documentId"]))
    assert document is not None
    assert document.tenant_id == "tenant-a"
    assert document.created_by == "user-a"

    app.dependency_overrides.pop(get_current_actor)
    unauthenticated = await client.post(
        "/api/assistant/v1/knowledge/search",
        json={"query": "inventory", "topK": 5},
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content", "document_key", "suffix"),
    [
        (
            "guide.pdf",
            "application/pdf",
            make_pdf(["PDF policy with enough searchable content. " * 4]),
            "pdf-guide",
            ".pdf",
        ),
        (
            "guide.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            make_docx(
                "<w:p><w:r><w:t>DOCX policy with enough searchable content.</w:t></w:r></w:p>"
            ),
            "docx-guide",
            ".docx",
        ),
        (
            "guide.md",
            "text/markdown",
            b"# Markdown policy\n\nEnough searchable content for the internal pilot.",
            "markdown-guide",
            ".md",
        ),
        (
            "guide.html",
            "text/html",
            b"<!doctype html><html><body><p>HTML policy content.</p></body></html>",
            "html-guide",
            ".html",
        ),
    ],
)
async def test_upload_accepts_supported_content_types_independent_of_extension(
    knowledge_api: KnowledgeApiContext,
    filename: str,
    media_type: str,
    content: bytes,
    document_key: str,
    suffix: str,
) -> None:
    client, _, storage, _ = knowledge_api

    response = await _upload(
        client,
        content=content,
        document_key=document_key,
        filename=f"renamed-{filename}.ignored",
        media_type=media_type,
    )

    assert response.status_code == 202
    assert response.json()["mediaType"] == media_type
    assert response.json()["pageCount"] is None or media_type == "application/pdf"
    assert any(key.endswith(f"original{suffix}") for key in storage.objects)


@pytest.mark.asyncio
async def test_upload_validation_and_global_gate(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, _, _, _ = knowledge_api
    wrong_type = await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={"file": ("note.txt", b"not pdf", "text/plain")},
        data={},
    )
    assert wrong_type.status_code == 415
    mismatch = await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={"file": ("renamed.pdf", b"# Actually markdown", "application/pdf")},
        data={},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "MEDIA_TYPE_MISMATCH"
    global_upload = await _upload(client, scope="GLOBAL")
    assert global_upload.status_code == 404


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
        previous_job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document_id)
        )
        assert previous_job is not None
        previous_job.cleanup_required = True
        previous_job.cleanup_artifacts = [{"kind": "vector_document", "document_id": str(uuid4())}]
        await session.commit()
    retried = await client.post(
        f"/api/assistant/v1/knowledge/documents/{document_id}/retry",
        json={},
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == "QUEUED"
    async with factory() as session:
        retry_job = await session.scalar(
            select(KnowledgeIndexJob)
            .where(KnowledgeIndexJob.document_id == document_id)
            .order_by(KnowledgeIndexJob.created_at.desc())
        )
    assert retry_job is not None and retry_job.cleanup_required is True
    assert retry_job.cleanup_artifacts == previous_job.cleanup_artifacts

    async with factory() as session:
        document = await session.get(KnowledgeDocument, document_id)
        assert document is not None
        document.status = DocumentStatus.OCR_REQUIRED.value
        await session.commit()
    ocr_retry = await client.post(
        f"/api/assistant/v1/knowledge/documents/{document_id}/retry",
        json={},
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
        "embeddingDeployment": "local-fake",
        "embeddingDimension": 256,
        "rerankDeployment": "local-fake",
        "providerBoundary": "INTERNAL",
        "productionReady": False,
        "requestCount": 0,
        "inputTokens": 0,
        "retrievalConfig": {
            "candidateMultiplier": 4,
            "embeddingDeployment": "local-fake",
            "embeddingDimension": 256,
            "embeddingModel": "fake-lexical-v1",
            "maxCandidates": 80,
            "providerBoundary": "INTERNAL",
            "rerankDeployment": "local-fake",
            "rerankModel": "fake-lexical-rerank-v2",
            "rerankThreshold": 0.1,
            "retrieval": "dense-bm25-rrf-v1",
            "rrfK": 60,
            "vectorIndex": "knowledge_chunks_hybrid_v2",
        },
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
        data_classification="ORDINARY",
        size_bytes=100,
        sha256=uuid4().hex + uuid4().hex,
        object_key=f"knowledge/{document_id}/original.pdf",
        page_count=1,
        status=DocumentStatus.READY.value,
        active=True,
        publication_status="PUBLISHED",
        retention_status="RETAINED",
        index_artifacts_present=True,
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
        embedding_deployment="local-fake",
        embedding_dimension=256,
    )
