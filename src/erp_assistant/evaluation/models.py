import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from erp_assistant.rag.retriever import (
    ActorContext,
    RetrievalOutcome,
    RetrievalRequest,
)
from erp_assistant.rag.types import SearchDecision


class EvaluationCategory(StrEnum):
    CHINESE_NATURAL_LANGUAGE = "CHINESE_NATURAL_LANGUAGE"
    ENGLISH_ABBREVIATION = "ENGLISH_ABBREVIATION"
    SKU_PRODUCT_IDENTIFIER = "SKU_PRODUCT_IDENTIFIER"
    NUMERIC_TABLE_FACT = "NUMERIC_TABLE_FACT"
    AMBIGUOUS_QUESTION = "AMBIGUOUS_QUESTION"
    NO_ANSWER = "NO_ANSWER"
    TENANT_ISOLATION = "TENANT_ISOLATION"


class DatasetProvenance(StrEnum):
    BUSINESS = "BUSINESS"
    SYNTHETIC = "SYNTHETIC"


class GateDecision(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class RelevantSource:
    document_key: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    dataset_version: str
    question: str
    actor: ActorContext
    expected_sources: tuple[RelevantSource, ...]
    expected_decision: SearchDecision
    categories: frozenset[EvaluationCategory]
    forbidden_document_keys: frozenset[str]
    provenance: DatasetProvenance
    reviewed: bool


@dataclass(frozen=True)
class EvaluationDataset:
    version: str
    content_hash: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True)
class ObservedCase:
    case: EvaluationCase
    outcome: RetrievalOutcome


@dataclass(frozen=True)
class EvaluationEnvironment:
    index_identity: str
    parser_version: str
    chunk_version: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in vars(self).values()):
            raise ValueError("Evaluation environment identities must not be empty")


@dataclass(frozen=True)
class EvaluationMetrics:
    case_count: int
    evaluated_case_count: int
    recall_at_5: float | None
    mrr_at_5: float | None
    ndcg_at_5: float | None
    refusal_accuracy: float | None
    tenant_leakage_count: int
    latency_p50_ms: float
    latency_p95_ms: float


@dataclass(frozen=True)
class EvaluationMetadata:
    dataset_version: str
    dataset_hash: str
    index_identity: str
    parser_version: str
    chunk_version: str
    retrieval_pipeline: str | None
    embedding_model: str | None
    rerank_model: str | None
    configuration_fingerprint: str | None
    comparison_key: str
    executed_at: str
    elapsed_ms: float


@dataclass(frozen=True)
class EvaluationFailure:
    case_id: str
    code: str
    kind: str


@dataclass(frozen=True)
class QualityGate:
    decision: GateDecision
    production_baseline_approved: bool
    framework_complete: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationReport:
    metrics: EvaluationMetrics
    metadata: EvaluationMetadata
    failures: tuple[EvaluationFailure, ...]
    category_metrics: dict[str, EvaluationMetrics]
    gate: QualityGate

    def to_dict(self) -> dict[str, Any]:
        return {
            "reportVersion": "retrieval-evaluation-v1",
            "metadata": {
                "datasetVersion": self.metadata.dataset_version,
                "datasetHash": self.metadata.dataset_hash,
                "executedAt": self.metadata.executed_at,
                "elapsedMs": self.metadata.elapsed_ms,
                "indexIdentity": self.metadata.index_identity,
                "parserVersion": self.metadata.parser_version,
                "chunkVersion": self.metadata.chunk_version,
                "retrievalPipeline": self.metadata.retrieval_pipeline,
                "embeddingModel": self.metadata.embedding_model,
                "rerankModel": self.metadata.rerank_model,
                "configurationFingerprint": self.metadata.configuration_fingerprint,
                "comparisonKey": self.metadata.comparison_key,
            },
            "metrics": _metrics_dict(self.metrics),
            "categoryMetrics": {
                category: _metrics_dict(metrics)
                for category, metrics in self.category_metrics.items()
            },
            "failures": [
                {"caseId": failure.case_id, "code": failure.code, "kind": failure.kind}
                for failure in self.failures
            ],
            "gate": {
                "decision": self.gate.decision,
                "productionBaselineApproved": self.gate.production_baseline_approved,
                "frameworkComplete": self.gate.framework_complete,
                "reasons": list(self.gate.reasons),
            },
        }

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class EvaluationDatasetError(ValueError):
    def __init__(self, code: str, message: str, line_number: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line_number = line_number


class Retriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome: ...


def _metrics_dict(metrics: EvaluationMetrics) -> dict[str, float | int | None]:
    return {
        "caseCount": metrics.case_count,
        "evaluatedCaseCount": metrics.evaluated_case_count,
        "recallAt5": metrics.recall_at_5,
        "mrrAt5": metrics.mrr_at_5,
        "ndcgAt5": metrics.ndcg_at_5,
        "refusalAccuracy": metrics.refusal_accuracy,
        "tenantLeakageCount": metrics.tenant_leakage_count,
        "latencyP50Ms": metrics.latency_p50_ms,
        "latencyP95Ms": metrics.latency_p95_ms,
    }
