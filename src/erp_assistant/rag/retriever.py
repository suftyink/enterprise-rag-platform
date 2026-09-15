import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument
from erp_assistant.orchestration.providers import EmbeddingProvider, RerankProvider
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_eligibility import retrieval_eligibility_filters
from erp_assistant.rag.types import KnowledgeScope, SearchDecision
from erp_assistant.rag.vector_store import VectorStore


@dataclass(frozen=True)
class ActorContext:
    subject_id: str
    tenant_id: str


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    actor: ActorContext
    evidence_limit: int = 5


@dataclass(frozen=True)
class RetrievalEvidence:
    chunk_id: UUID
    document_id: UUID
    document_key: str
    title: str
    scope: KnowledgeScope
    document_version: int
    page_start: int
    page_end: int
    section_title: str | None
    content: str
    score: float
    retrieval_rank: int


@dataclass(frozen=True)
class RetrievalRun:
    trace_id: UUID
    retrieval: str
    embedding_model: str
    rerank_model: str
    threshold: float
    config_fingerprint: str
    elapsed_ms: float


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
    ) -> None:
        self._session = session
        self._vector_store = vector_store
        self._embedding = embedding
        self._reranker = reranker
        self._settings = settings

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        request = self._normalize_request(request)
        trace_id = uuid4()
        started = perf_counter()
        evidence = await self._retrieve_evidence(request)
        return RetrievalOutcome(
            decision=(
                SearchDecision.MATCHED if evidence else SearchDecision.INSUFFICIENT_KNOWLEDGE
            ),
            evidence=evidence,
            run=RetrievalRun(
                trace_id=trace_id,
                retrieval="dense-bm25-rrf-v1",
                embedding_model=self._embedding.model,
                rerank_model=self._reranker.model,
                threshold=self._settings.rag_rerank_threshold,
                config_fingerprint=self._config_fingerprint(),
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            ),
        )

    async def _retrieve_evidence(self, request: RetrievalRequest) -> tuple[RetrievalEvidence, ...]:
        candidate_limit = min(
            self._settings.rag_max_candidates,
            max(20, request.evidence_limit * self._settings.rag_candidate_multiplier),
        )
        try:
            vector = (await self._embedding.embed([request.query]))[0]
        except Exception as exc:
            raise RagDependencyError("EMBEDDING_FAILED", "查询向量生成失败") from exc
        try:
            hits = await asyncio.to_thread(
                self._vector_store.search,
                request.query,
                vector,
                request.actor.tenant_id,
                candidate_limit,
                self._settings.rag_rrf_k,
            )
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
                        or_(
                            KnowledgeDocument.scope == KnowledgeScope.GLOBAL.value,
                            (
                                (KnowledgeDocument.scope == KnowledgeScope.TENANT.value)
                                & (KnowledgeDocument.scope_id == request.actor.tenant_id)
                            ),
                        ),
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
            or not 1 <= request.evidence_limit <= 20
        ):
            raise RagError("INVALID_RETRIEVAL_REQUEST", "检索请求无效")
        return RetrievalRequest(
            query=query,
            actor=ActorContext(subject_id=subject_id, tenant_id=tenant_id),
            evidence_limit=request.evidence_limit,
        )

    def _config_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "candidateMultiplier": self._settings.rag_candidate_multiplier,
                "embeddingDimension": self._embedding.dimension,
                "embeddingModel": self._embedding.model,
                "maxCandidates": self._settings.rag_max_candidates,
                "rerankModel": self._reranker.model,
                "rerankThreshold": self._settings.rag_rerank_threshold,
                "retrieval": "dense-bm25-rrf-v1",
                "rrfK": self._settings.rag_rrf_k,
                "vectorCollection": self._settings.milvus_collection,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(payload).hexdigest()
