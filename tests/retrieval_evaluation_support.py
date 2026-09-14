import json
from pathlib import Path
from uuid import UUID

from erp_assistant.evaluation.retrieval import EvaluationEnvironment
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import (
    RetrievalEvidence,
    RetrievalOutcome,
    RetrievalRequest,
    RetrievalRun,
)
from erp_assistant.rag.types import KnowledgeScope, SearchDecision


class DeterministicRetriever:
    def __init__(self, outcomes: dict[str, RetrievalOutcome]) -> None:
        self._outcomes = outcomes

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        return self._outcomes[request.query]


class FailIfCalledRetriever:
    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        raise AssertionError(f"Retriever must not run for invalid dataset: {request.query}")


class PartiallyFailingRetriever:
    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        if request.query == "dependency failure":
            raise RagDependencyError("MILVUS_SEARCH_FAILED", "must not enter report")
        return outcome("inventory-sop")


def evidence(
    document_key: str,
    *,
    page_start: int = 1,
    page_end: int = 1,
) -> RetrievalEvidence:
    key_number = sum(ord(character) for character in document_key)
    return RetrievalEvidence(
        chunk_id=UUID(int=key_number),
        document_id=UUID(int=key_number + 10_000),
        document_key=document_key,
        title="Evaluation document",
        scope=KnowledgeScope.TENANT,
        document_version=1,
        page_start=page_start,
        page_end=page_end,
        section_title=None,
        content="Sensitive evidence body must not enter reports.",
        score=0.95,
        retrieval_rank=1,
    )


def outcome(
    *document_keys: str,
    decision: SearchDecision = SearchDecision.MATCHED,
    elapsed_ms: float = 10.0,
) -> RetrievalOutcome:
    return RetrievalOutcome(
        decision=decision,
        evidence=tuple(evidence(key) for key in document_keys),
        run=RetrievalRun(
            trace_id=UUID(int=1),
            retrieval="dense-bm25-rrf-v1",
            embedding_model="embedding-v1",
            rerank_model="rerank-v1",
            threshold=0.1,
            config_fingerprint="a" * 64,
            elapsed_ms=elapsed_ms,
        ),
    )


def evaluation_case(
    case_id: str,
    question: str,
    *,
    expected_sources: list[dict[str, object]],
    expected_decision: str,
    categories: list[str],
    forbidden_document_keys: list[str] | None = None,
    provenance: str = "SYNTHETIC",
    reviewed: bool = False,
) -> dict[str, object]:
    return {
        "datasetVersion": "2026-09-15.1",
        "id": case_id,
        "question": question,
        "actor": {"subjectId": "evaluation-user", "tenantId": "tenant-a"},
        "expectedSources": expected_sources,
        "expectedDecision": expected_decision,
        "categories": categories,
        "forbiddenDocumentKeys": forbidden_document_keys or [],
        "provenance": provenance,
        "reviewed": reviewed,
    }


def write_dataset(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


def environment(**changes: str) -> EvaluationEnvironment:
    values = {
        "index_identity": "knowledge_chunks_hybrid_v1:2026-09-15",
        "parser_version": "pdf-parser-v1",
        "chunk_version": "page-aware-v1",
    }
    values.update(changes)
    return EvaluationEnvironment(**values)
