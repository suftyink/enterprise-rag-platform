import json
from uuid import UUID

import httpx

from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import (
    RetrievalEvidence,
    RetrievalOutcome,
    RetrievalRequest,
    RetrievalRun,
)
from erp_assistant.rag.types import KnowledgeScope, SearchDecision


class HttpRetrievalAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        try:
            response = await self._client.post(
                "/api/assistant/v1/knowledge/search",
                json={
                    "tenantId": request.actor.tenant_id,
                    "operatorId": request.actor.subject_id,
                    "query": request.query,
                    "topK": request.evidence_limit,
                },
            )
        except httpx.RequestError as exc:
            raise RagDependencyError(
                "HTTP_TRANSPORT_FAILED", "Retrieval deployment transport failed"
            ) from exc
        if response.status_code in {401, 403}:
            raise RagDependencyError(
                "HTTP_AUTH_FAILED", "Retrieval deployment authentication failed"
            )
        if response.is_error:
            raise RagDependencyError(
                "HTTP_RESPONSE_FAILED", "Retrieval deployment returned an error"
            )
        try:
            payload = response.json()
            pipeline = payload["pipeline"]
            evidence = tuple(
                RetrievalEvidence(
                    chunk_id=UUID(item["chunkId"]),
                    document_id=UUID(item["documentId"]),
                    document_key=item["documentKey"],
                    title=item["title"],
                    scope=KnowledgeScope(item["scope"]),
                    document_version=int(item["version"]),
                    page_start=int(item["pageStart"]),
                    page_end=int(item["pageEnd"]),
                    section_title=item.get("sectionTitle"),
                    content=item["content"],
                    score=float(item["score"]),
                    retrieval_rank=int(item["retrievalRank"]),
                )
                for item in payload["results"]
            )
            return RetrievalOutcome(
                decision=SearchDecision(payload["decision"]),
                evidence=evidence,
                run=RetrievalRun(
                    trace_id=UUID(payload["traceId"]),
                    retrieval=pipeline["retrieval"],
                    embedding_model=pipeline["embeddingModel"],
                    rerank_model=pipeline["rerankModel"],
                    threshold=float(pipeline["threshold"]),
                    config_fingerprint=pipeline["configFingerprint"],
                    elapsed_ms=float(payload["elapsedMs"]),
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RagDependencyError(
                "HTTP_INVALID_RESPONSE", "Retrieval deployment response is invalid"
            ) from exc
