import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings
from erp_assistant.evaluation.models import (
    EvaluationEnvironment,
    EvaluationFailureKind,
    EvaluationReport,
    QualityThresholds,
)
from erp_assistant.evaluation.profiles import (
    ProfileActivation,
    RetrievalProfile,
    SqlRetrievalProfileStore,
)
from erp_assistant.evaluation.retrieval import RetrievalEvaluator
from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalProfileRecord,
)
from erp_assistant.orchestration.http_retrieval_providers import (
    HttpEmbeddingProvider,
    HttpRerankProvider,
)
from erp_assistant.orchestration.providers import ProviderBoundary
from erp_assistant.rag.chunker import ChunkDraft
from erp_assistant.rag.classified_retriever import ClassifiedRetrieverRouter
from erp_assistant.rag.profile_bound_retriever import ProfileBoundRetriever
from erp_assistant.rag.retriever import KnowledgeRetriever, RetrievalOutcome
from erp_assistant.rag.types import DataClassification, SearchDecision
from erp_assistant.rag.vector_store import VectorHit
from tests.retrieval_evaluation_support import (
    DeterministicRetriever,
    environment,
    evaluation_case,
    outcome,
    write_dataset,
)


class OneHitVectorStore:
    identity = "staging-index-v1"
    dimension = 3

    def __init__(self, chunk_id: UUID) -> None:
        self._chunk_id = chunk_id

    def search(
        self,
        query: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        del query, vector, tenant_id, limit, rrf_k
        return [VectorHit(chunk_id=self._chunk_id, score=0.9)]

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[ChunkDraft],
        vectors: list[list[float]],
    ) -> None:
        del document_id, scope, scope_id, version, chunks, vectors

    def delete_document(self, document_id: UUID) -> None:
        del document_id


class FailIfCalledActivator:
    async def activate(self, profile_id: UUID, report: EvaluationReport) -> ProfileActivation:
        del profile_id, report
        raise AssertionError("A profile that missed a gate must not be activated")


@pytest_asyncio.fixture
async def retrieval_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    document_id = uuid4()
    chunk_id = uuid4()
    async with factory() as session:
        session.add(
            KnowledgeDocument(
                id=document_id,
                scope="TENANT",
                scope_id="tenant-a",
                tenant_id="tenant-a",
                document_key="semantic-sop",
                version=1,
                title="Semantic SOP",
                original_filename="semantic.md",
                media_type="text/markdown",
                size_bytes=100,
                sha256="a" * 64,
                object_key="knowledge/semantic.md",
                status="READY",
                active=True,
                publication_status="PUBLISHED",
                retention_status="RETAINED",
                index_artifacts_present=True,
                created_by="evaluation-user",
            )
        )
        session.add(
            KnowledgeChunk(
                id=chunk_id,
                document_id=document_id,
                scope="TENANT",
                scope_id="tenant-a",
                version=1,
                chunk_index=0,
                page_start=1,
                page_end=1,
                content="Expedited dispatch means priority shipment for SKU-X1.",
                content_hash="b" * 64,
                char_count=56,
                embedding_model="semantic-embed:v1",
                heading_path=[],
                block_kind="PARAGRAPH",
                source_locator={"pageStart": 1, "pageEnd": 1},
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def profile_store() -> AsyncIterator[SqlRetrievalProfileStore]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield SqlRetrievalProfileStore(factory)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_routes_restricted_cases_only_to_the_internal_path(tmp_path: Path) -> None:
    question = "restricted compensation policy"
    dataset = write_dataset(
        tmp_path / "restricted.jsonl",
        [
            evaluation_case(
                "restricted-1",
                question,
                expected_sources=[{"documentKey": "restricted-sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
                data_classification="RESTRICTED",
            )
        ],
    )
    cloud = DeterministicRetriever({})
    internal = DeterministicRetriever({question: outcome("restricted-sop")})
    router = ClassifiedRetrieverRouter(
        {
            DataClassification.ORDINARY: cloud,
            DataClassification.RESTRICTED: internal,
        }
    )

    report = await RetrievalEvaluator(router, environment()).run(dataset)

    assert report.metrics.recall_at_5 == 1.0


@pytest.mark.asyncio
async def test_run_fails_closed_when_restricted_route_is_unavailable(tmp_path: Path) -> None:
    dataset = write_dataset(
        tmp_path / "restricted-unavailable.jsonl",
        [
            evaluation_case(
                "restricted-1",
                "restricted policy",
                expected_sources=[{"documentKey": "restricted-sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
                data_classification="RESTRICTED",
            )
        ],
    )
    router = ClassifiedRetrieverRouter({DataClassification.ORDINARY: DeterministicRetriever({})})

    report = await RetrievalEvaluator(router, environment()).run(dataset)

    assert [(failure.code, failure.kind) for failure in report.failures] == [
        ("RESTRICTED_ROUTE_UNAVAILABLE", EvaluationFailureKind.RETRIEVAL_DEPENDENCY)
    ]


@pytest.mark.asyncio
async def test_run_uses_real_http_profile_and_records_safe_usage(
    tmp_path: Path,
    retrieval_session: AsyncSession,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
                    "usage": {"prompt_tokens": 7},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.96}],
                "usage": {"total_tokens": 11},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://approved-cloud.example",
        transport=httpx.MockTransport(handler),
        timeout=1,
    ) as client:
        embedding = HttpEmbeddingProvider(
            client,
            deployment="cloud-embedding-prod",
            model="semantic-embed",
            version="v1",
            dimension=3,
            boundary=ProviderBoundary.CLOUD,
        )
        reranker = HttpRerankProvider(
            client,
            deployment="cloud-rerank-prod",
            model="semantic-rerank",
            version="v2",
            boundary=ProviderBoundary.CLOUD,
        )
        chunk_id = await retrieval_session.scalar(select(KnowledgeChunk.id))
        assert chunk_id is not None
        retriever = KnowledgeRetriever(
            session=retrieval_session,
            vector_store=OneHitVectorStore(chunk_id),
            embedding=embedding,
            reranker=reranker,
            settings=Settings(rag_enabled=True, rag_rerank_threshold=0.5),
        )
        question = "How do I send an order faster?"
        dataset = write_dataset(
            tmp_path / "semantic.jsonl",
            [
                evaluation_case(
                    "semantic-1",
                    question,
                    expected_sources=[
                        {"documentKey": "semantic-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=["CHINESE_NATURAL_LANGUAGE", "SKU_PRODUCT_IDENTIFIER"],
                )
            ],
        )

        report = await RetrievalEvaluator(retriever, environment()).run(dataset)

    assert report.metrics.recall_at_5 == 1.0
    assert requests == ["/embeddings", "/rerank"]
    assert len(report.metadata.routes) == 1
    route = report.metadata.routes[0]
    assert route.embedding_deployment == "cloud-embedding-prod"
    assert route.embedding_dimension == 3
    assert route.rerank_deployment == "cloud-rerank-prod"
    assert route.provider_boundary is ProviderBoundary.CLOUD
    assert route.request_count == 2
    assert route.input_tokens == 18
    assert question not in json.dumps(report.to_dict())


@pytest.mark.asyncio
async def test_run_uses_only_the_atomically_active_production_binding(
    tmp_path: Path,
    retrieval_session: AsyncSession,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/embeddings":
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.2, 0.3, 0.5]}]},
            )
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.97}]},
        )

    async with httpx.AsyncClient(
        base_url="https://approved-cloud.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        embedding = HttpEmbeddingProvider(
            client,
            deployment="cloud-embedding-prod",
            model="semantic-embed",
            version="v1",
            dimension=3,
            boundary=ProviderBoundary.CLOUD,
        )
        reranker = HttpRerankProvider(
            client,
            deployment="cloud-rerank-prod",
            model="semantic-rerank",
            version="v2",
            boundary=ProviderBoundary.CLOUD,
        )
        chunk_id = await retrieval_session.scalar(select(KnowledgeChunk.id))
        assert chunk_id is not None
        vector_store = OneHitVectorStore(chunk_id)
        settings = Settings(rag_enabled=True, app_env="production")
        raw_retriever = KnowledgeRetriever(
            session=retrieval_session,
            vector_store=vector_store,
            embedding=embedding,
            reranker=reranker,
            settings=settings,
        )
        active = RetrievalProfileRecord(
            profile_key="ordinary-production",
            version=1,
            data_classification="ORDINARY",
            provider_boundary="CLOUD",
            embedding_deployment="cloud-embedding-prod",
            embedding_model="semantic-embed:v1",
            embedding_dimension=3,
            rerank_deployment="cloud-rerank-prod",
            rerank_model="semantic-rerank:v2",
            index_identity=vector_store.identity,
            parser_version="parser-v1",
            chunk_version="chunk-v1",
            retrieval_config={},
            configuration_fingerprint=raw_retriever.configuration_fingerprint(),
            production_ready=True,
            status="ACTIVE",
        )
        retrieval_session.add(active)
        await retrieval_session.commit()
        dataset = write_dataset(
            tmp_path / "active-binding.jsonl",
            [
                evaluation_case(
                    "active-binding-1",
                    "semantic query",
                    expected_sources=[
                        {"documentKey": "semantic-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=["CHINESE_NATURAL_LANGUAGE"],
                )
            ],
        )
        retriever = ProfileBoundRetriever(retrieval_session, raw_retriever, settings)

        first = await RetrievalEvaluator(retriever, environment()).run(dataset)
        active.configuration_fingerprint = "f" * 64
        await retrieval_session.commit()
        requests.clear()
        second = await RetrievalEvaluator(retriever, environment()).run(dataset)

    assert first.metrics.recall_at_5 == 1.0
    assert requests == []
    assert [failure.code for failure in second.failures] == ["ACTIVE_PROFILE_BINDING_MISMATCH"]


@pytest.mark.asyncio
async def test_run_never_sends_restricted_query_to_cloud_profile(
    tmp_path: Path,
    retrieval_session: AsyncSession,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://approved-cloud.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        embedding = HttpEmbeddingProvider(
            client,
            deployment="cloud-embedding-prod",
            model="semantic-embed",
            version="v1",
            dimension=3,
            boundary=ProviderBoundary.CLOUD,
        )
        reranker = HttpRerankProvider(
            client,
            deployment="cloud-rerank-prod",
            model="semantic-rerank",
            version="v2",
            boundary=ProviderBoundary.CLOUD,
        )
        chunk_id = await retrieval_session.scalar(select(KnowledgeChunk.id))
        assert chunk_id is not None
        retriever = KnowledgeRetriever(
            session=retrieval_session,
            vector_store=OneHitVectorStore(chunk_id),
            embedding=embedding,
            reranker=reranker,
            settings=Settings(rag_enabled=True),
        )
        dataset = write_dataset(
            tmp_path / "restricted-cloud.jsonl",
            [
                evaluation_case(
                    "restricted-cloud-1",
                    "restricted payroll policy",
                    expected_sources=[
                        {"documentKey": "semantic-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=["CHINESE_NATURAL_LANGUAGE"],
                    data_classification="RESTRICTED",
                )
            ],
        )

        report = await RetrievalEvaluator(retriever, environment()).run(dataset)

    assert requests == []
    assert [failure.code for failure in report.failures] == ["RESTRICTED_ROUTE_FORBIDDEN"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_code"),
    [
        ("timeout", "PROVIDER_TIMEOUT"),
        ("auth", "PROVIDER_AUTH_FAILED"),
        ("rate-limit", "PROVIDER_RATE_LIMITED"),
        ("invalid", "PROVIDER_INVALID_RESPONSE"),
        ("rerank-auth", "PROVIDER_AUTH_FAILED"),
    ],
)
async def test_run_preserves_normalized_provider_failure_codes(
    tmp_path: Path,
    retrieval_session: AsyncSession,
    failure_mode: str,
    expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure_mode == "timeout":
            raise httpx.ReadTimeout("provider timeout", request=request)
        if request.url.path == "/embeddings" and failure_mode == "auth":
            return httpx.Response(401)
        if request.url.path == "/embeddings" and failure_mode == "rate-limit":
            return httpx.Response(429)
        if request.url.path == "/embeddings" and failure_mode == "invalid":
            return httpx.Response(200, json=[])
        if request.url.path == "/rerank" and failure_mode == "rerank-auth":
            return httpx.Response(401)
        if request.url.path == "/embeddings":
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.2, 0.3, 0.5]}],
                    "usage": {"prompt_tokens": 2},
                },
            )
        raise AssertionError(f"Unexpected request: {request.url.path}")

    async with httpx.AsyncClient(
        base_url="https://approved-cloud.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        embedding = HttpEmbeddingProvider(
            client,
            deployment="cloud-embedding-prod",
            model="semantic-embed",
            version="v1",
            dimension=3,
            boundary=ProviderBoundary.CLOUD,
        )
        chunk_id = await retrieval_session.scalar(select(KnowledgeChunk.id))
        assert chunk_id is not None
        retriever = KnowledgeRetriever(
            session=retrieval_session,
            vector_store=OneHitVectorStore(chunk_id),
            embedding=embedding,
            reranker=HttpRerankProvider(
                client,
                deployment="cloud-rerank-prod",
                model="semantic-rerank",
                version="v2",
                boundary=ProviderBoundary.CLOUD,
            ),
            settings=Settings(rag_enabled=True),
        )
        dataset = write_dataset(
            tmp_path / f"{failure_mode}.jsonl",
            [
                evaluation_case(
                    f"{failure_mode}-1",
                    "semantic query",
                    expected_sources=[
                        {"documentKey": "semantic-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=["CHINESE_NATURAL_LANGUAGE"],
                )
            ],
        )

        report = await RetrievalEvaluator(retriever, environment()).run(dataset)

    assert [failure.code for failure in report.failures] == [expected_code]


@pytest.mark.asyncio
async def test_run_rejects_fake_provider_identity_as_a_production_profile(
    tmp_path: Path,
) -> None:
    question = "fake-only query"
    dataset = write_dataset(
        tmp_path / "fake-profile.jsonl",
        [
            evaluation_case(
                "fake-profile-1",
                question,
                expected_sources=[{"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            )
        ],
    )
    fake_outcome = outcome("approved-sop")
    fake_outcome = replace(
        fake_outcome,
        run=replace(
            fake_outcome.run,
            embedding_model="fake-lexical-v1",
            embedding_deployment="local-fake",
            rerank_model="fake-lexical-rerank-v2",
            rerank_deployment="local-fake",
            production_ready=False,
        ),
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever({question: fake_outcome}), environment()
    ).run(dataset)

    assert report.gate.decision.value == "FAIL"
    assert [failure.code for failure in report.failures] == ["UNAPPROVED_PRODUCTION_PROFILE"]


@pytest.mark.asyncio
async def test_run_rejects_query_model_that_does_not_match_index_dimension(
    tmp_path: Path,
    retrieval_session: AsyncSession,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://approved-cloud.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        embedding = HttpEmbeddingProvider(
            client,
            deployment="cloud-embedding-prod",
            model="semantic-embed",
            version="v2",
            dimension=2,
            boundary=ProviderBoundary.CLOUD,
        )
        chunk_id = await retrieval_session.scalar(select(KnowledgeChunk.id))
        assert chunk_id is not None
        retriever = KnowledgeRetriever(
            session=retrieval_session,
            vector_store=OneHitVectorStore(chunk_id),
            embedding=embedding,
            reranker=HttpRerankProvider(
                client,
                deployment="cloud-rerank-prod",
                model="semantic-rerank",
                version="v2",
                boundary=ProviderBoundary.CLOUD,
            ),
            settings=Settings(rag_enabled=True),
        )
        dataset = write_dataset(
            tmp_path / "dimension-mismatch.jsonl",
            [
                evaluation_case(
                    "dimension-mismatch-1",
                    "semantic query",
                    expected_sources=[
                        {"documentKey": "semantic-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=["CHINESE_NATURAL_LANGUAGE"],
                )
            ],
        )

        report = await RetrievalEvaluator(retriever, environment()).run(dataset)

    assert requests == []
    assert [failure.code for failure in report.failures] == ["INDEX_BINDING_MISMATCH"]


def _passing_business_scenario() -> tuple[list[dict[str, object]], dict[str, RetrievalOutcome]]:
    categories = [
        "CHINESE_NATURAL_LANGUAGE",
        "ENGLISH_ABBREVIATION",
        "SKU_PRODUCT_IDENTIFIER",
        "NUMERIC_TABLE_FACT",
        "AMBIGUOUS_QUESTION",
        "NO_ANSWER",
        "TENANT_ISOLATION",
    ]
    cases: list[dict[str, object]] = []
    outcomes: dict[str, RetrievalOutcome] = {}
    for index in range(100):
        category = categories[index % len(categories)]
        question = f"approved business question {index}"
        if category == "NO_ANSWER":
            cases.append(
                evaluation_case(
                    f"case-{index}",
                    question,
                    expected_sources=[],
                    expected_decision="INSUFFICIENT_KNOWLEDGE",
                    categories=[category],
                    provenance="BUSINESS",
                    reviewed=True,
                )
            )
            value = outcome(decision=SearchDecision.INSUFFICIENT_KNOWLEDGE)
        else:
            cases.append(
                evaluation_case(
                    f"case-{index}",
                    question,
                    expected_sources=[
                        {"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}
                    ],
                    expected_decision="MATCHED",
                    categories=[category],
                    forbidden_document_keys=(
                        ["tenant-b-secret"] if category == "TENANT_ISOLATION" else []
                    ),
                    provenance="BUSINESS",
                    reviewed=True,
                )
            )
            value = outcome("approved-sop")
        outcomes[question] = replace(
            value,
            run=replace(
                value.run,
                embedding_model="semantic-embed:v1",
                embedding_deployment="cloud-embedding-prod",
                embedding_dimension=3,
                rerank_model="semantic-rerank:v2",
                rerank_deployment="cloud-rerank-prod",
                provider_boundary=ProviderBoundary.CLOUD,
            ),
        )
    return cases, outcomes


@pytest.mark.asyncio
async def test_run_activates_only_passing_staging_profiles_and_supports_rollback(
    tmp_path: Path,
    profile_store: SqlRetrievalProfileStore,
) -> None:
    cases, raw_outcomes = _passing_business_scenario()
    outcomes = {key: value for key, value in raw_outcomes.items()}
    dataset = write_dataset(tmp_path / "passing-profile.jsonl", cases)
    first_id = uuid4()
    second_id = uuid4()

    def profile(profile_id: UUID, version: int) -> RetrievalProfile:
        return RetrievalProfile(
            id=profile_id,
            profile_key="ordinary-pilot",
            version=version,
            data_classification=DataClassification.ORDINARY,
            provider_boundary=ProviderBoundary.CLOUD,
            embedding_deployment="cloud-embedding-prod",
            embedding_model="semantic-embed:v1",
            embedding_dimension=3,
            rerank_deployment="cloud-rerank-prod",
            rerank_model="semantic-rerank:v2",
            index_identity=f"staging-index-v{version}",
            parser_version="parser-v1",
            chunk_version="chunk-v1",
            retrieval_config={"candidateCount": 40, "rrfK": 60, "rerankThreshold": 0.5},
            configuration_fingerprint="a" * 64,
        )

    first = profile(first_id, 1)
    second = profile(second_id, 2)
    await profile_store.stage(first)
    await profile_store.stage(second)

    async def evaluate(candidate: RetrievalProfile) -> EvaluationReport:
        return await RetrievalEvaluator(
            DeterministicRetriever(outcomes),
            environment(
                index_identity=candidate.index_identity,
                parser_version=candidate.parser_version,
                chunk_version=candidate.chunk_version,
            ),
            activator=profile_store,
            candidate_profile_id=candidate.id,
        ).run(dataset)

    first_report = await evaluate(first)
    second_report = await evaluate(second)
    rollback_report = await evaluate(first)

    assert first_report.activation is not None
    assert first_report.activation.previous_profile_id is None
    assert second_report.activation is not None
    assert second_report.activation.previous_profile_id == str(first_id)
    assert rollback_report.activation is not None
    assert rollback_report.activation.profile_id == str(first_id)
    assert rollback_report.activation.previous_profile_id == str(second_id)


@pytest.mark.asyncio
async def test_run_rejects_profile_when_passing_evidence_does_not_match_binding(
    tmp_path: Path,
    profile_store: SqlRetrievalProfileStore,
) -> None:
    cases, outcomes = _passing_business_scenario()
    dataset = write_dataset(tmp_path / "binding-mismatch.jsonl", cases)
    profile_id = uuid4()
    await profile_store.stage(
        RetrievalProfile(
            id=profile_id,
            profile_key="ordinary-staging",
            version=1,
            data_classification=DataClassification.ORDINARY,
            provider_boundary=ProviderBoundary.CLOUD,
            embedding_deployment="cloud-embedding-prod",
            embedding_model="semantic-embed:v1",
            embedding_dimension=4,
            rerank_deployment="cloud-rerank-prod",
            rerank_model="semantic-rerank:v2",
            index_identity="staging-index-v1",
            parser_version="parser-v1",
            chunk_version="chunk-v1",
            retrieval_config={},
            configuration_fingerprint="a" * 64,
        )
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever(outcomes),
        environment(
            index_identity="staging-index-v1",
            parser_version="parser-v1",
            chunk_version="chunk-v1",
        ),
        activator=profile_store,
        candidate_profile_id=profile_id,
    ).run(dataset)

    assert report.activation is None
    assert report.gate.decision.value == "FAIL"
    assert [failure.code for failure in report.failures] == ["PROFILE_EVIDENCE_MISMATCH"]


@pytest.mark.asyncio
async def test_run_blocks_profile_when_configured_latency_gate_is_missed(
    tmp_path: Path,
) -> None:
    question = "slow semantic query"
    dataset = write_dataset(
        tmp_path / "slow.jsonl",
        [
            evaluation_case(
                "slow-1",
                question,
                expected_sources=[{"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            )
        ],
    )
    evaluation_environment = EvaluationEnvironment(
        index_identity="staging-index-v1",
        parser_version="parser-v1",
        chunk_version="chunk-v1",
        thresholds=QualityThresholds(latency_p95_ms=500),
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever({question: outcome("approved-sop", elapsed_ms=501)}),
        evaluation_environment,
        activator=FailIfCalledActivator(),
        candidate_profile_id=uuid4(),
    ).run(dataset)

    assert "LATENCY_P95_ABOVE_THRESHOLD" in report.gate.reasons
    assert report.activation is None
