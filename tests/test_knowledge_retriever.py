from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument
from erp_assistant.orchestration.fake_providers import (
    FakeEmbeddingProvider,
    FakeRerankProvider,
)
from erp_assistant.orchestration.providers import RankedDocument
from erp_assistant.rag.chunker import ChunkDraft
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.retriever import ActorContext, KnowledgeRetriever, RetrievalRequest
from erp_assistant.rag.types import DocumentStatus, SearchDecision
from erp_assistant.rag.vector_store import MilvusVectorStore, VectorHit


@dataclass
class RecordingVectorStore:
    hits: list[VectorHit] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

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
        del vector, tenant_id, rrf_k
        self.queries.append(query)
        return self.hits[:limit]


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
async def test_retrieve_returns_typed_evidence_for_a_permitted_document(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _ready_document("TENANT", "tenant-a", "inventory-policy")
    chunk = _chunk(document, "Inventory policy keeps safety stock available.")
    vector_store = RecordingVectorStore(hits=[VectorHit(chunk_id=chunk.id, score=0.9)])
    async with session_factory() as session:
        session.add_all([document, chunk])
        await session.commit()
        retriever = KnowledgeRetriever(
            session=session,
            vector_store=vector_store,
            embedding=FakeEmbeddingProvider(),
            reranker=FakeRerankProvider(),
            settings=Settings(rag_enabled=True),
        )

        outcome = await retriever.retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
                evidence_limit=5,
            )
        )

    assert outcome.decision is SearchDecision.MATCHED
    assert len(outcome.evidence) == 1
    assert outcome.evidence[0].document_key == "inventory-policy"
    assert outcome.evidence[0].content == "Inventory policy keeps safety stock available."
    assert outcome.evidence[0].retrieval_rank == 1
    assert outcome.run.retrieval == "dense-bm25-rrf-v1"
    assert outcome.run.embedding_model == "fake-lexical-v1"
    assert outcome.run.rerank_model == "fake-lexical-rerank-v2"


@pytest.mark.asyncio
async def test_retrieve_reports_database_failure_as_a_dependency_error() -> None:
    vector_store = RecordingVectorStore(hits=[VectorHit(chunk_id=uuid4(), score=0.9)])
    retriever = KnowledgeRetriever(
        session=FailingSession(),  # type: ignore[arg-type]
        vector_store=vector_store,
        embedding=FakeEmbeddingProvider(),
        reranker=FakeRerankProvider(),
        settings=Settings(rag_enabled=True),
    )

    with pytest.raises(RagDependencyError) as raised:
        await retriever.retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    assert raised.value.code == "DATABASE_SEARCH_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retrieval_request",
    [
        RetrievalRequest(
            query="   ",
            actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
        ),
        RetrievalRequest(
            query="inventory",
            actor=ActorContext(subject_id="user-a", tenant_id=""),
        ),
        RetrievalRequest(
            query="inventory",
            actor=ActorContext(subject_id="", tenant_id="tenant-a"),
        ),
        RetrievalRequest(
            query="inventory",
            actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            evidence_limit=0,
        ),
        RetrievalRequest(
            query="inventory",
            actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            evidence_limit=21,
        ),
    ],
)
async def test_retrieve_rejects_invalid_domain_requests(
    retrieval_request: RetrievalRequest,
) -> None:
    retriever = KnowledgeRetriever(
        session=FailingSession(),  # type: ignore[arg-type]
        vector_store=FailingVectorStore(),
        embedding=FailingEmbeddingProvider(),
        reranker=FailingRerankProvider(),
        settings=Settings(rag_enabled=True),
    )

    with pytest.raises(RagError) as raised:
        await retriever.retrieve(retrieval_request)

    assert raised.value.code == "INVALID_RETRIEVAL_REQUEST"


@pytest.mark.asyncio
async def test_retrieve_normalizes_query_before_all_retrieval_dependencies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vector_store = RecordingVectorStore()
    async with session_factory() as session:
        await _retriever(session, vector_store).retrieve(
            RetrievalRequest(
                query="  ＩＮＶＥＮＴＯＲＹ\t Policy \n",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    assert vector_store.queries == ["inventory policy"]


@pytest.mark.asyncio
async def test_retrieve_returns_only_ready_active_permitted_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    permitted = _ready_document("TENANT", "tenant-a", "tenant-policy")
    global_document = _ready_document("GLOBAL", "GLOBAL", "global-policy")
    other_tenant = _ready_document("TENANT", "tenant-b", "other-policy")
    inactive = _ready_document("TENANT", "tenant-a", "inactive-policy")
    inactive.active = False
    failed = _ready_document("TENANT", "tenant-a", "failed-policy")
    failed.status = DocumentStatus.FAILED.value
    review_pending = _ready_document("TENANT", "tenant-a", "pending-policy")
    review_pending.publication_status = "REVIEW_PENDING"
    documents = [
        permitted,
        global_document,
        other_tenant,
        inactive,
        failed,
        review_pending,
    ]
    chunks = [_chunk(document, "inventory policy evidence") for document in documents]
    vector_store = RecordingVectorStore(
        hits=[VectorHit(chunk_id=chunk.id, score=0.9) for chunk in chunks]
    )
    async with session_factory() as session:
        session.add_all([*documents, *chunks])
        await session.commit()
        outcome = await _retriever(session, vector_store).retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    assert {evidence.document_key for evidence in outcome.evidence} == {
        "tenant-policy",
        "global-policy",
    }


@pytest.mark.asyncio
async def test_retrieve_returns_insufficient_knowledge_below_threshold(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _ready_document("TENANT", "tenant-a", "inventory-policy")
    chunk = _chunk(document, "inventory policy evidence")
    vector_store = RecordingVectorStore(hits=[VectorHit(chunk_id=chunk.id, score=0.9)])
    async with session_factory() as session:
        session.add_all([document, chunk])
        await session.commit()
        outcome = await _retriever(session, vector_store).retrieve(
            RetrievalRequest(
                query="unrelated question",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    assert outcome.decision is SearchDecision.INSUFFICIENT_KNOWLEDGE
    assert outcome.evidence == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_dependency", "expected_code"),
    [
        ("embedding", "EMBEDDING_FAILED"),
        ("vector_store", "MILVUS_SEARCH_FAILED"),
        ("reranker", "RERANK_FAILED"),
    ],
)
async def test_retrieve_reports_boundary_dependency_failures(
    session_factory: async_sessionmaker[AsyncSession],
    failing_dependency: str,
    expected_code: str,
) -> None:
    document = _ready_document("TENANT", "tenant-a", "inventory-policy")
    chunk = _chunk(document, "inventory policy evidence")
    vector_store = RecordingVectorStore(hits=[VectorHit(chunk_id=chunk.id, score=0.9)])
    embedding = FakeEmbeddingProvider()
    reranker = FakeRerankProvider()
    if failing_dependency == "embedding":
        embedding = FailingEmbeddingProvider()  # type: ignore[assignment]
    elif failing_dependency == "vector_store":
        vector_store = FailingVectorStore()
    else:
        reranker = FailingRerankProvider()  # type: ignore[assignment]

    async with session_factory() as session:
        session.add_all([document, chunk])
        await session.commit()
        retriever = KnowledgeRetriever(
            session=session,
            vector_store=vector_store,
            embedding=embedding,
            reranker=reranker,
            settings=Settings(rag_enabled=True),
        )
        with pytest.raises(RagDependencyError) as raised:
            await retriever.retrieve(
                RetrievalRequest(
                    query="inventory policy",
                    actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
                )
            )

    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_retrieve_rejects_incomplete_rerank_responses(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_document = _ready_document("TENANT", "tenant-a", "first-policy")
    second_document = _ready_document("TENANT", "tenant-a", "second-policy")
    first_chunk = _chunk(first_document, "inventory policy first")
    second_chunk = _chunk(second_document, "inventory policy second")
    vector_store = RecordingVectorStore(
        hits=[
            VectorHit(chunk_id=first_chunk.id, score=0.9),
            VectorHit(chunk_id=second_chunk.id, score=0.8),
        ]
    )
    async with session_factory() as session:
        session.add_all([first_document, second_document, first_chunk, second_chunk])
        await session.commit()
        retriever = KnowledgeRetriever(
            session=session,
            vector_store=vector_store,
            embedding=FakeEmbeddingProvider(),
            reranker=IncompleteReranker(),
            settings=Settings(rag_enabled=True),
        )
        with pytest.raises(RagDependencyError) as raised:
            await retriever.retrieve(
                RetrievalRequest(
                    query="inventory policy",
                    actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
                )
            )

    assert raised.value.code == "RERANK_INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
async def test_retrieve_rejects_non_finite_rerank_scores(
    session_factory: async_sessionmaker[AsyncSession],
    score: float,
) -> None:
    document = _ready_document("TENANT", "tenant-a", "inventory-policy")
    chunk = _chunk(document, "inventory policy evidence")
    async with session_factory() as session:
        session.add_all([document, chunk])
        await session.commit()
        retriever = KnowledgeRetriever(
            session=session,
            vector_store=RecordingVectorStore(hits=[VectorHit(chunk_id=chunk.id, score=0.9)]),
            embedding=FakeEmbeddingProvider(),
            reranker=InvalidScoreReranker(score),
            settings=Settings(rag_enabled=True),
        )
        with pytest.raises(RagDependencyError) as raised:
            await retriever.retrieve(
                RetrievalRequest(
                    query="inventory policy",
                    actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
                )
            )

    assert raised.value.code == "RERANK_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_retrieve_orders_tied_evidence_independently_of_vector_hit_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_document = _ready_document("TENANT", "tenant-a", "first-policy")
    second_document = _ready_document("TENANT", "tenant-a", "second-policy")
    first_chunk = _chunk(
        first_document,
        "inventory policy first",
        chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    second_chunk = _chunk(
        second_document,
        "inventory policy second",
        chunk_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    async with session_factory() as session:
        session.add_all([first_document, second_document, first_chunk, second_chunk])
        await session.commit()
        first_outcome = await KnowledgeRetriever(
            session=session,
            vector_store=RecordingVectorStore(
                hits=[
                    VectorHit(chunk_id=first_chunk.id, score=0.9),
                    VectorHit(chunk_id=second_chunk.id, score=0.8),
                ]
            ),
            embedding=FakeEmbeddingProvider(),
            reranker=TieReranker(),
            settings=Settings(rag_enabled=True),
        ).retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )
        reversed_outcome = await KnowledgeRetriever(
            session=session,
            vector_store=RecordingVectorStore(
                hits=[
                    VectorHit(chunk_id=second_chunk.id, score=0.8),
                    VectorHit(chunk_id=first_chunk.id, score=0.9),
                ]
            ),
            embedding=FakeEmbeddingProvider(),
            reranker=TieReranker(),
            settings=Settings(rag_enabled=True),
        ).retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    expected = [first_chunk.id, second_chunk.id]
    assert [evidence.chunk_id for evidence in first_outcome.evidence] == expected
    assert [evidence.chunk_id for evidence in reversed_outcome.evidence] == expected


@pytest.mark.asyncio
async def test_retrieve_uses_dense_and_bm25_rrf_through_the_milvus_adapter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _ready_document("TENANT", "tenant-a", "hybrid-policy")
    chunk = _chunk(document, "inventory policy hybrid evidence")
    client = RecordingMilvusClient(chunk.id)
    vector_store = StubMilvusVectorStore(client)
    async with session_factory() as session:
        session.add_all([document, chunk])
        await session.commit()
        outcome = await KnowledgeRetriever(
            session=session,
            vector_store=vector_store,
            embedding=FakeEmbeddingProvider(),
            reranker=FakeRerankProvider(),
            settings=Settings(rag_enabled=True),
        ).retrieve(
            RetrievalRequest(
                query="inventory policy",
                actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
            )
        )

    assert outcome.decision is SearchDecision.MATCHED
    assert [evidence.document_key for evidence in outcome.evidence] == ["hybrid-policy"]
    assert client.anns_fields == ["dense_vector", "sparse_vector"]
    assert client.ranker == {"strategy": "rrf", "params": {"k": 60}}


def test_milvus_adapter_namespaces_vector_rows_but_returns_source_chunk_id() -> None:
    chunk_id = uuid4()
    client = RecordingMilvusClient(chunk_id)
    vector_store = StubMilvusVectorStore(client)
    chunk = ChunkDraft(
        id=chunk_id,
        index=0,
        page_start=1,
        page_end=1,
        section_title=None,
        content="inventory policy",
        content_hash="a" * 64,
    )

    vector_store.upsert_document(uuid4(), "TENANT", "tenant-a", 1, [chunk], [[0.1]])
    vector_store.upsert_document(uuid4(), "TENANT", "tenant-a", 1, [chunk], [[0.1]])

    assert len(client.upsert_rows) == 2
    assert client.upsert_rows[0]["id"] != client.upsert_rows[1]["id"]
    assert {row["chunk_id"] for row in client.upsert_rows} == {str(chunk_id)}


@pytest.mark.asyncio
async def test_retrieve_fingerprints_the_effective_retrieval_configuration(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vector_store = RecordingVectorStore()
    request = RetrievalRequest(
        query="inventory policy",
        actor=ActorContext(subject_id="user-a", tenant_id="tenant-a"),
    )
    async with session_factory() as session:
        first = await _retriever(session, vector_store).retrieve(request)
        repeated = await _retriever(session, vector_store).retrieve(request)
        changed = await KnowledgeRetriever(
            session=session,
            vector_store=vector_store,
            embedding=FakeEmbeddingProvider(),
            reranker=FakeRerankProvider(),
            settings=Settings(rag_enabled=True, rag_rerank_threshold=0.25),
        ).retrieve(request)

    assert len(first.run.config_fingerprint) == 64
    assert repeated.run.config_fingerprint == first.run.config_fingerprint
    assert changed.run.config_fingerprint != first.run.config_fingerprint


class FailingSession:
    async def execute(self, statement: object) -> None:
        del statement
        raise RuntimeError("database unavailable")


class FailingEmbeddingProvider:
    model = "failing-embedding"
    dimension = 1

    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("embedding unavailable")


class FailingVectorStore(RecordingVectorStore):
    def search(
        self,
        query: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        del query, vector, tenant_id, limit, rrf_k
        raise RuntimeError("vector store unavailable")


class FailingRerankProvider:
    model = "failing-reranker"

    async def rerank(self, query: str, documents: list[str]) -> list[object]:
        del query, documents
        raise RuntimeError("reranker unavailable")


class IncompleteReranker:
    model = "incomplete-reranker"

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]:
        del query, documents
        return [RankedDocument(index=0, score=0.5)]


class InvalidScoreReranker:
    model = "invalid-score-reranker"

    def __init__(self, score: float) -> None:
        self.score = score

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]:
        del query, documents
        return [RankedDocument(index=0, score=self.score)]


class TieReranker:
    model = "tie-reranker"

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]:
        del query
        return [RankedDocument(index=index, score=0.5) for index in range(len(documents))]


class RecordingMilvusClient:
    def __init__(self, chunk_id: UUID) -> None:
        self.chunk_id = chunk_id
        self.anns_fields: list[str] = []
        self.ranker: dict[str, object] = {}
        self.upsert_rows: list[dict[str, object]] = []

    def has_collection(self, collection: str) -> bool:
        del collection
        return True

    def hybrid_search(
        self,
        collection: str,
        *,
        reqs: list[object],
        ranker: object,
        limit: int,
        output_fields: list[str],
    ) -> list[list[dict[str, object]]]:
        del collection, limit, output_fields
        self.anns_fields = [request.anns_field for request in reqs]
        self.ranker = ranker.dict()
        return [
            [
                {
                    "id": str(uuid4()),
                    "distance": 0.9,
                    "entity": {"chunk_id": str(self.chunk_id)},
                }
            ]
        ]

    def delete(self, collection: str, *, filter: str) -> None:
        del collection, filter

    def upsert(self, collection: str, *, data: list[dict[str, object]]) -> None:
        del collection
        self.upsert_rows.extend(data)

    def close(self) -> None:
        pass


class StubMilvusVectorStore(MilvusVectorStore):
    def __init__(self, client: RecordingMilvusClient) -> None:
        super().__init__("unused", "knowledge", FakeEmbeddingProvider.dimension)
        self._stub_client = client

    def _client(self) -> RecordingMilvusClient:  # type: ignore[override]
        return self._stub_client


def _retriever(
    session: AsyncSession,
    vector_store: RecordingVectorStore,
) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        session=session,
        vector_store=vector_store,
        embedding=FakeEmbeddingProvider(),
        reranker=FakeRerankProvider(),
        settings=Settings(rag_enabled=True),
    )


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


def _chunk(
    document: KnowledgeDocument,
    content: str,
    *,
    chunk_id: UUID | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id or uuid4(),
        document_id=document.id,
        scope=document.scope,
        scope_id=document.scope_id,
        version=document.version,
        chunk_index=0,
        page_start=1,
        page_end=1,
        content=content,
        content_hash=uuid4().hex + uuid4().hex,
        char_count=len(content),
        embedding_model="fake-lexical-v1",
        embedding_deployment="local-fake",
        embedding_dimension=FakeEmbeddingProvider.dimension,
    )
