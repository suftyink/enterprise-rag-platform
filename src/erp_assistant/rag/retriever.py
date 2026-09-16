import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from math import isfinite
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    KnowledgeScopeRef,
)
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument
from erp_assistant.orchestration.providers import (
    EmbeddingProvider,
    ProviderBoundary,
    ProviderUsage,
    RerankProvider,
)
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_eligibility import (
    authorized_scope_filter,
    retrieval_eligibility_filters,
)
from erp_assistant.rag.types import DataClassification, KnowledgeScope, SearchDecision
from erp_assistant.rag.vector_store import VectorStore

__all__ = [
    "ActorContext",
    "KnowledgeRetriever",
    "RetrievalEvidence",
    "RetrievalOutcome",
    "RetrievalRequest",
    "RetrievalRun",
    "retrieval_configuration",
    "retrieval_configuration_fingerprint",
]


def retrieval_configuration_fingerprint(
    config: dict[str, int | float | bool | str],
) -> str:
    payload = json.dumps(
        config,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def retrieval_configuration(
    *,
    settings: Settings,
    vector_store: VectorStore,
    embedding: EmbeddingProvider,
    reranker: RerankProvider,
) -> dict[str, int | float | bool | str]:
    embedding_boundary = ProviderBoundary(getattr(embedding, "boundary", ProviderBoundary.INTERNAL))
    rerank_boundary = ProviderBoundary(getattr(reranker, "boundary", embedding_boundary))
    if embedding_boundary is not rerank_boundary:
        raise RagDependencyError(
            "PROFILE_BOUNDARY_MISMATCH", "Embedding 与 Rerank 不属于同一安全边界"
        )
    return {
        "candidateMultiplier": settings.rag_candidate_multiplier,
        "embeddingDimension": embedding.dimension,
        "embeddingDeployment": str(getattr(embedding, "deployment", "legacy")),
        "embeddingModel": embedding.model,
        "maxCandidates": settings.rag_max_candidates,
        "rerankModel": reranker.model,
        "rerankDeployment": str(getattr(reranker, "deployment", "legacy")),
        "rerankThreshold": settings.rag_rerank_threshold,
        "retrieval": "dense-bm25-rrf-v1",
        "rrfK": settings.rag_rrf_k,
        "vectorIndex": str(getattr(vector_store, "identity", settings.milvus_collection)),
        "providerBoundary": embedding_boundary.value,
    }


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    actor: ActorContext
    evidence_limit: int = 5
    data_classification: DataClassification = DataClassification.ORDINARY
    trace_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class RetrievalEvidence:
    chunk_id: UUID
    document_id: UUID
    document_key: str
    title: str
    scope: KnowledgeScope
    document_version: int
    page_start: int | None
    page_end: int | None
    section_title: str | None
    content: str
    score: float
    retrieval_rank: int
    heading_path: tuple[str, ...] = ()
    anchor: str | None = None
    block_kind: str = "PARAGRAPH"
    source_locator: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalRun:
    trace_id: UUID
    retrieval: str
    embedding_model: str
    rerank_model: str
    threshold: float
    config_fingerprint: str
    elapsed_ms: float
    embedding_deployment: str = "legacy"
    embedding_dimension: int = 0
    rerank_deployment: str = "legacy"
    provider_boundary: ProviderBoundary = ProviderBoundary.INTERNAL
    production_ready: bool = True
    request_count: int = 0
    input_tokens: int = 0
    retrieval_config: dict[str, int | float | bool | str] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalOutcome:
    decision: SearchDecision
    evidence: tuple[RetrievalEvidence, ...]
    run: RetrievalRun


class KnowledgeRetriever:
    def __init__(
        self,
        *,
        session: AsyncSession,
        vector_store: VectorStore,
        embedding: EmbeddingProvider,
        reranker: RerankProvider,
        settings: Settings,
        identity_access: IdentityAccess,
    ) -> None:
        self._session = session
        self._vector_store = vector_store
        self._embedding = embedding
        self._reranker = reranker
        self._settings = settings
        self._identity = identity_access

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        request = self._normalize_request(request)
        authorized_scopes = await self._authorize(request)
        self._reset_provider_usage()
        if request.data_classification is DataClassification.UNCLASSIFIED:
            raise RagDependencyError(
                "DATA_CLASSIFICATION_REQUIRED", "未分类知识不能进入检索 Provider"
            )
        boundary = self._provider_boundary()
        if (
            request.data_classification is DataClassification.RESTRICTED
            and boundary is not ProviderBoundary.INTERNAL
        ):
            raise RagDependencyError(
                "RESTRICTED_ROUTE_FORBIDDEN", "受限知识只能使用批准的内网或本地模型"
            )
        vector_dimension = getattr(self._vector_store, "dimension", self._embedding.dimension)
        if vector_dimension != self._embedding.dimension:
            raise RagDependencyError(
                "INDEX_BINDING_MISMATCH", "查询 Embedding 维度与活动索引绑定不一致"
            )
        started = perf_counter()
        evidence = await self._retrieve_evidence(request, authorized_scopes)
        await self._authorize(request)
        return RetrievalOutcome(
            decision=(
                SearchDecision.MATCHED if evidence else SearchDecision.INSUFFICIENT_KNOWLEDGE
            ),
            evidence=evidence,
            run=RetrievalRun(
                trace_id=request.trace_id,
                retrieval="dense-bm25-rrf-v1",
                embedding_model=self._embedding.model,
                rerank_model=self._reranker.model,
                threshold=self._settings.rag_rerank_threshold,
                config_fingerprint=self.configuration_fingerprint(),
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
                embedding_deployment=str(getattr(self._embedding, "deployment", "legacy")),
                embedding_dimension=self._embedding.dimension,
                rerank_deployment=str(getattr(self._reranker, "deployment", "legacy")),
                provider_boundary=boundary,
                production_ready=bool(
                    getattr(self._embedding, "production_ready", True)
                    and getattr(self._reranker, "production_ready", True)
                ),
                request_count=self._provider_usage().requests,
                input_tokens=self._provider_usage().input_tokens,
                retrieval_config=self.retrieval_config(),
            ),
        )

    async def _retrieve_evidence(
        self,
        request: RetrievalRequest,
        authorized_scopes: tuple[KnowledgeScopeRef, ...],
    ) -> tuple[RetrievalEvidence, ...]:
        candidate_limit = min(
            self._settings.rag_max_candidates,
            max(20, request.evidence_limit * self._settings.rag_candidate_multiplier),
        )
        index_filter = KnowledgeChunk.index_identity == self.index_identity
        if self._settings.app_env.casefold() != "production":
            index_filter = or_(index_filter, KnowledgeChunk.index_identity == "legacy")
        scope_filter = authorized_scope_filter(authorized_scopes)
        try:
            vector = (await self._embedding.embed([request.query]))[0]
        except RagDependencyError:
            raise
        except Exception as exc:
            raise RagDependencyError("EMBEDDING_FAILED", "查询向量生成失败") from exc
        try:
            hits = await asyncio.to_thread(
                self._vector_store.search,
                request.query,
                vector,
                authorized_scopes,
                candidate_limit,
                self._settings.rag_rrf_k,
            )
        except RagDependencyError:
            raise
        except Exception as exc:
            raise RagDependencyError("MILVUS_SEARCH_FAILED", "Milvus 检索失败") from exc
        if not hits:
            return ()

        try:
            rows = (
                await self._session.execute(
                    select(KnowledgeChunk, KnowledgeDocument)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                    .where(
                        KnowledgeChunk.id.in_([hit.chunk_id for hit in hits]),
                        *retrieval_eligibility_filters(),
                        KnowledgeDocument.data_classification == request.data_classification.value,
                        KnowledgeChunk.embedding_model == self._embedding.model,
                        KnowledgeChunk.embedding_deployment
                        == str(getattr(self._embedding, "deployment", "legacy")),
                        KnowledgeChunk.embedding_dimension == self._embedding.dimension,
                        index_filter,
                        scope_filter,
                    )
                )
            ).all()
        except Exception as exc:
            raise RagDependencyError("DATABASE_SEARCH_FAILED", "知识元数据查询失败") from exc

        by_id = {chunk.id: (chunk, document) for chunk, document in rows}
        candidates: list[tuple[KnowledgeChunk, KnowledgeDocument, int, float]] = []
        for retrieval_rank, hit in enumerate(hits, start=1):
            row = by_id.get(hit.chunk_id)
            if row is not None:
                candidates.append((*row, retrieval_rank, hit.score))
        if not candidates:
            return ()

        try:
            ranked = await self._reranker.rerank(
                request.query,
                [chunk.content for chunk, _, _, _ in candidates],
            )
        except RagDependencyError:
            raise
        except Exception as exc:
            raise RagDependencyError("RERANK_FAILED", "候选结果重排失败") from exc

        scored_evidence: list[tuple[RetrievalEvidence, float]] = []
        seen_indexes: set[int] = set()
        for ranked_document in ranked:
            if (
                ranked_document.index < 0
                or ranked_document.index >= len(candidates)
                or ranked_document.index in seen_indexes
                or not isfinite(ranked_document.score)
            ):
                raise RagDependencyError("RERANK_INVALID_RESPONSE", "候选结果重排响应无效")
            seen_indexes.add(ranked_document.index)
            if ranked_document.score < self._settings.rag_rerank_threshold:
                continue
            chunk, document, retrieval_rank, retrieval_score = candidates[ranked_document.index]
            scored_evidence.append(
                (
                    RetrievalEvidence(
                        chunk_id=chunk.id,
                        document_id=document.id,
                        document_key=document.document_key,
                        title=document.title,
                        scope=KnowledgeScope(document.scope),
                        document_version=document.version,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        section_title=chunk.section_title,
                        heading_path=tuple(chunk.heading_path),
                        anchor=chunk.anchor,
                        block_kind=chunk.block_kind,
                        source_locator=chunk.source_locator,
                        content=chunk.content,
                        score=ranked_document.score,
                        retrieval_rank=retrieval_rank,
                    ),
                    retrieval_score,
                )
            )
        if len(seen_indexes) != len(candidates):
            raise RagDependencyError("RERANK_INVALID_RESPONSE", "候选结果重排响应无效")
        scored_evidence.sort(key=lambda item: (-item[0].score, -item[1], str(item[0].chunk_id)))
        return tuple(item[0] for item in scored_evidence[: request.evidence_limit])

    @staticmethod
    def _normalize_request(request: RetrievalRequest) -> RetrievalRequest:
        query = unicodedata.normalize("NFKC", request.query)
        query = re.sub(r"\s+", " ", query).strip().casefold()
        subject_id = request.actor.subject_id.strip()
        tenant_id = request.actor.tenant_id.strip()
        if (
            not query
            or len(query) > 2_000
            or not subject_id
            or len(subject_id) > 64
            or not tenant_id
            or len(tenant_id) > 64
            or not request.actor.is_trusted
            or not 1 <= request.evidence_limit <= 20
        ):
            raise RagError("INVALID_RETRIEVAL_REQUEST", "检索请求无效")
        return RetrievalRequest(
            query=query,
            actor=request.actor,
            evidence_limit=request.evidence_limit,
            data_classification=request.data_classification,
            trace_id=request.trace_id,
        )

    async def _authorize(
        self, request: RetrievalRequest
    ) -> tuple[KnowledgeScopeRef, ...]:
        try:
            return await self._identity.authorize_read_scopes(
                request.actor,
                IdentityAction.SEARCH,
                trace_id=request.trace_id,
            )
        except IdentityDenied as exc:
            raise RagError(exc.code, exc.message) from exc

    @property
    def index_identity(self) -> str:
        return str(getattr(self._vector_store, "identity", self._settings.milvus_collection))

    def configuration_fingerprint(self) -> str:
        return retrieval_configuration_fingerprint(self.retrieval_config())

    def retrieval_config(self) -> dict[str, int | float | bool | str]:
        return retrieval_configuration(
            settings=self._settings,
            vector_store=self._vector_store,
            embedding=self._embedding,
            reranker=self._reranker,
        )

    def _provider_boundary(self) -> ProviderBoundary:
        embedding = ProviderBoundary(
            getattr(self._embedding, "boundary", ProviderBoundary.INTERNAL)
        )
        reranker = ProviderBoundary(getattr(self._reranker, "boundary", embedding))
        if embedding is not reranker:
            raise RagDependencyError(
                "PROFILE_BOUNDARY_MISMATCH", "Embedding 与 Rerank 不属于同一安全边界"
            )
        return embedding

    def _provider_usage(self) -> ProviderUsage:
        embedding = getattr(self._embedding, "usage", ProviderUsage())
        reranker = getattr(self._reranker, "usage", ProviderUsage())
        return ProviderUsage(
            requests=embedding.requests + reranker.requests,
            input_tokens=embedding.input_tokens + reranker.input_tokens,
        )

    def _reset_provider_usage(self) -> None:
        for provider in (self._embedding, self._reranker):
            reset = getattr(provider, "reset_usage", None)
            if callable(reset):
                reset()
