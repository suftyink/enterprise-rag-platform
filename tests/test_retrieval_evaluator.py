import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from erp_assistant.evaluation.retrieval import (
    EvaluationDatasetError,
    EvaluationEnvironment,
    GateDecision,
    HttpRetrievalAdapter,
    RetrievalEvaluator,
)
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
        return _outcome("inventory-sop")


def _evidence(
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


def _outcome(
    *document_keys: str,
    decision: SearchDecision = SearchDecision.MATCHED,
    elapsed_ms: float = 10.0,
) -> RetrievalOutcome:
    return RetrievalOutcome(
        decision=decision,
        evidence=tuple(_evidence(key) for key in document_keys),
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


def _case(
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


def _write_dataset(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


def _environment(**changes: str) -> EvaluationEnvironment:
    values = {
        "index_identity": "knowledge_chunks_hybrid_v1:2026-09-15",
        "parser_version": "pdf-parser-v1",
        "chunk_version": "page-aware-v1",
    }
    values.update(changes)
    return EvaluationEnvironment(**values)


@pytest.mark.asyncio
async def test_run_evaluates_versioned_dataset_with_retriever_and_reports_metrics(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path / "evaluation.jsonl",
        [
            _case(
                "inventory-1",
                "inventory lookup",
                expected_sources=[
                    {"documentKey": "inventory-sop", "pageStart": 1, "pageEnd": 2}
                ],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            ),
            _case(
                "unknown-1",
                "unknown policy",
                expected_sources=[],
                expected_decision="INSUFFICIENT_KNOWLEDGE",
                categories=["NO_ANSWER"],
            ),
        ],
    )
    retriever = DeterministicRetriever(
        {
            "inventory lookup": _outcome("inventory-sop", elapsed_ms=10),
            "unknown policy": _outcome(
                decision=SearchDecision.INSUFFICIENT_KNOWLEDGE,
                elapsed_ms=20,
            ),
        }
    )

    report = await RetrievalEvaluator(retriever, _environment()).run(dataset)

    assert report.metrics.case_count == 2
    assert report.metrics.evaluated_case_count == 2
    assert report.metrics.recall_at_5 == 1.0
    assert report.metrics.mrr_at_5 == 1.0
    assert report.metrics.ndcg_at_5 == 1.0
    assert report.metrics.refusal_accuracy == 1.0
    assert report.metrics.tenant_leakage_count == 0
    assert report.metrics.latency_p50_ms == 10
    assert report.metrics.latency_p95_ms == 20
    assert report.metadata.dataset_version == "2026-09-15.1"
    assert len(report.metadata.dataset_hash) == 64
    assert report.metadata.index_identity == "knowledge_chunks_hybrid_v1:2026-09-15"
    assert report.metadata.parser_version == "pdf-parser-v1"
    assert report.metadata.chunk_version == "page-aware-v1"
    assert report.metadata.retrieval_pipeline == "dense-bm25-rrf-v1"
    assert report.metadata.embedding_model == "embedding-v1"
    assert report.metadata.rerank_model == "rerank-v1"
    assert report.metadata.configuration_fingerprint == "a" * 64
    assert len(report.metadata.comparison_key) == 64
    assert report.gate.decision == GateDecision.INDETERMINATE
    assert "REQUIRED_CATEGORIES_INCOMPLETE" in report.gate.reasons
    assert "PRODUCTION_CASE_COUNT_TOO_LOW" in report.gate.reasons
    assert "NON_BUSINESS_CASES" in report.gate.reasons
    assert "UNREVIEWED_CASES" in report.gate.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda cases: cases.append(dict(cases[0])), "DUPLICATE_CASE_ID"),
        (
            lambda cases: cases[0].update(
                {"actor": {"subjectId": "", "tenantId": "tenant-a"}}
            ),
            "INVALID_EVALUATION_ACTOR",
        ),
        (lambda cases: cases[0].pop("expectedSources"), "INVALID_CASE_SCHEMA"),
        (lambda cases: cases[0].update({"unexpected": True}), "INVALID_CASE_SCHEMA"),
        (lambda cases: cases[0].update({"categories": []}), "INVALID_CASE_SCHEMA"),
        (
            lambda cases: cases.append(
                {**cases[0], "id": "case-2", "datasetVersion": "different-version"}
            ),
            "MIXED_DATASET_VERSION",
        ),
    ],
)
async def test_run_rejects_invalid_dataset_before_retrieval(
    tmp_path: Path,
    mutate: object,
    error_code: str,
) -> None:
    cases = [
        _case(
            "case-1",
            "question",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["SKU_PRODUCT_IDENTIFIER"],
        )
    ]
    assert callable(mutate)
    mutate(cases)
    dataset = _write_dataset(tmp_path / "invalid.jsonl", cases)

    with pytest.raises(EvaluationDatasetError) as raised:
        await RetrievalEvaluator(FailIfCalledRetriever(), _environment()).run(dataset)

    assert raised.value.code == error_code


@pytest.mark.asyncio
async def test_run_reports_dependency_failure_without_counting_it_as_missed_retrieval(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path / "dependency-failure.jsonl",
        [
            _case(
                "success-1",
                "successful retrieval",
                expected_sources=[
                    {"documentKey": "inventory-sop", "pageStart": 1, "pageEnd": 1}
                ],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            ),
            _case(
                "failure-1",
                "dependency failure",
                expected_sources=[
                    {"documentKey": "shipping-sop", "pageStart": 1, "pageEnd": 1}
                ],
                expected_decision="MATCHED",
                categories=["ENGLISH_ABBREVIATION"],
            ),
        ],
    )

    report = await RetrievalEvaluator(PartiallyFailingRetriever(), _environment()).run(dataset)

    assert report.metrics.case_count == 2
    assert report.metrics.evaluated_case_count == 1
    assert report.metrics.recall_at_5 == 1.0
    assert [(failure.case_id, failure.code, failure.kind) for failure in report.failures] == [
        ("failure-1", "MILVUS_SEARCH_FAILED", "RETRIEVAL_DEPENDENCY")
    ]


@pytest.mark.asyncio
async def test_run_passes_reviewed_business_baseline_and_reports_required_categories(
    tmp_path: Path,
) -> None:
    category_cycle = [
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
        category = category_cycle[index % len(category_cycle)]
        question = f"business question {index}"
        if category == "NO_ANSWER":
            cases.append(
                _case(
                    f"case-{index}",
                    question,
                    expected_sources=[],
                    expected_decision="INSUFFICIENT_KNOWLEDGE",
                    categories=[category],
                    provenance="BUSINESS",
                    reviewed=True,
                )
            )
            outcomes[question] = _outcome(decision=SearchDecision.INSUFFICIENT_KNOWLEDGE)
        else:
            cases.append(
                _case(
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
            outcomes[question] = _outcome("approved-sop")
    dataset = _write_dataset(tmp_path / "production-baseline.jsonl", cases)

    report = await RetrievalEvaluator(
        DeterministicRetriever(outcomes), _environment()
    ).run(dataset)

    assert report.gate.decision == GateDecision.PASS
    assert report.gate.production_baseline_approved is True
    assert report.gate.reasons == ()
    assert set(report.category_metrics) == set(category_cycle)
    assert all(metric.case_count > 0 for metric in report.category_metrics.values())
    assert report.metrics.recall_at_5 == 1.0
    assert report.metrics.mrr_at_5 == 1.0
    assert report.metrics.refusal_accuracy == 1.0
    assert report.metrics.tenant_leakage_count == 0


@pytest.mark.asyncio
async def test_run_fails_quality_gate_for_low_quality_or_tenant_leakage(
    tmp_path: Path,
) -> None:
    cases = [
        _case(
            "miss-1",
            "missed evidence",
            expected_sources=[{"documentKey": "expected-sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["CHINESE_NATURAL_LANGUAGE"],
        ),
        _case(
            "refusal-1",
            "unsupported question",
            expected_sources=[],
            expected_decision="INSUFFICIENT_KNOWLEDGE",
            categories=["NO_ANSWER"],
        ),
        _case(
            "leak-1",
            "tenant probe",
            expected_sources=[{"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["TENANT_ISOLATION"],
            forbidden_document_keys=["tenant-b-secret"],
        ),
    ]
    outcomes = {
        "missed evidence": _outcome(
            decision=SearchDecision.INSUFFICIENT_KNOWLEDGE
        ),
        "unsupported question": _outcome("unrelated-doc"),
        "tenant probe": _outcome("tenant-b-secret"),
    }

    report = await RetrievalEvaluator(
        DeterministicRetriever(outcomes), _environment()
    ).run(_write_dataset(tmp_path / "failed-baseline.jsonl", cases))

    assert report.gate.decision == GateDecision.FAIL
    assert report.gate.production_baseline_approved is False
    assert set(report.gate.reasons) == {
        "TENANT_LEAKAGE",
        "RECALL_AT_5_BELOW_THRESHOLD",
        "MRR_AT_5_BELOW_THRESHOLD",
        "REFUSAL_ACCURACY_BELOW_THRESHOLD",
    }
    assert report.metrics.tenant_leakage_count == 1


@pytest.mark.asyncio
async def test_run_counts_duplicate_chunks_for_one_source_once_in_ndcg(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path / "duplicate-chunks.jsonl",
        [
            _case(
                "duplicate-chunks-1",
                "overlapping chunks",
                expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["NUMERIC_TABLE_FACT"],
            )
        ],
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever({"overlapping chunks": _outcome("sop", "sop")}),
        _environment(),
    ).run(dataset)

    assert report.metrics.recall_at_5 == 1.0
    assert report.metrics.ndcg_at_5 == 1.0


@pytest.mark.asyncio
async def test_run_publishes_reproducible_machine_readable_report_without_sensitive_text(
    tmp_path: Path,
) -> None:
    question = "confidential employee question"
    dataset = _write_dataset(
        tmp_path / "reproducible.jsonl",
        [
            _case(
                "case-1",
                question,
                expected_sources=[
                    {"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}
                ],
                expected_decision="MATCHED",
                categories=["SKU_PRODUCT_IDENTIFIER"],
            )
        ],
    )
    retriever = DeterministicRetriever({question: _outcome("approved-sop")})

    report = await RetrievalEvaluator(retriever, _environment()).run(dataset)
    same_report = await RetrievalEvaluator(retriever, _environment()).run(dataset)
    changed_report = await RetrievalEvaluator(
        retriever, _environment(index_identity="knowledge_chunks_hybrid_v2:2026-09-16")
    ).run(dataset)
    report_path = tmp_path / "report.json"
    report.write(report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["reportVersion"] == "retrieval-evaluation-v1"
    assert payload["metadata"]["datasetHash"] == report.metadata.dataset_hash
    assert payload["metrics"]["caseCount"] == 1
    assert payload["gate"]["decision"] == "INDETERMINATE"
    assert datetime.fromisoformat(payload["metadata"]["executedAt"]).tzinfo is not None
    assert payload["metadata"]["elapsedMs"] >= 0
    assert report.metadata.comparison_key == same_report.metadata.comparison_key
    assert report.metadata.comparison_key != changed_report.metadata.comparison_key
    assert question not in serialized
    assert "Sensitive evidence body" not in serialized


@pytest.mark.asyncio
async def test_run_can_use_http_adapter_and_classifies_authentication_failure(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path / "http.jsonl",
        [
            _case(
                "http-1",
                "deployed lookup",
                expected_sources=[
                    {"documentKey": "deployed-sop", "pageStart": 4, "pageEnd": 4}
                ],
                expected_decision="MATCHED",
                categories=["ENGLISH_ABBREVIATION"],
            )
        ],
    )

    async def successful_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assistant/v1/knowledge/search"
        return httpx.Response(
            200,
            json={
                "traceId": str(UUID(int=9)),
                "query": "deployed lookup",
                "decision": "MATCHED",
                "elapsedMs": 12.5,
                "pipeline": {
                    "retrieval": "dense-bm25-rrf-v1",
                    "embeddingModel": "embedding-http-v1",
                    "rerankModel": "rerank-http-v1",
                    "threshold": 0.1,
                    "configFingerprint": "b" * 64,
                },
                "results": [
                    {
                        "chunkId": str(UUID(int=10)),
                        "documentId": str(UUID(int=11)),
                        "documentKey": "deployed-sop",
                        "title": "Deployed SOP",
                        "scope": "TENANT",
                        "version": 3,
                        "pageStart": 4,
                        "pageEnd": 4,
                        "sectionTitle": None,
                        "content": "body",
                        "score": 0.9,
                        "retrievalRank": 1,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://assistant.example",
        transport=httpx.MockTransport(successful_handler),
    ) as client:
        report = await RetrievalEvaluator(
            HttpRetrievalAdapter(client), _environment()
        ).run(dataset)

    assert report.metrics.recall_at_5 == 1.0
    assert report.metadata.embedding_model == "embedding-http-v1"
    assert report.metadata.configuration_fingerprint == "b" * 64

    async with httpx.AsyncClient(
        base_url="https://assistant.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    ) as client:
        failed_report = await RetrievalEvaluator(
            HttpRetrievalAdapter(client), _environment()
        ).run(dataset)

    assert [
        (failure.code, failure.kind) for failure in failed_report.failures
    ] == [("HTTP_AUTH_FAILED", "DEPLOYMENT_EXECUTION")]
    assert failed_report.gate.decision == GateDecision.FAIL


@pytest.mark.asyncio
async def test_run_rejects_mixed_retrieval_identity_as_execution_failure(
    tmp_path: Path,
) -> None:
    cases = [
        _case(
            "identity-1",
            "first identity",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["CHINESE_NATURAL_LANGUAGE"],
        ),
        _case(
            "identity-2",
            "changed identity",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["ENGLISH_ABBREVIATION"],
        ),
    ]
    original = _outcome("sop")
    changed = replace(
        original,
        run=replace(original.run, config_fingerprint="c" * 64),
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever(
            {"first identity": original, "changed identity": changed}
        ),
        _environment(),
    ).run(_write_dataset(tmp_path / "mixed-identity.jsonl", cases))

    assert report.metrics.evaluated_case_count == 1
    assert [(failure.case_id, failure.code) for failure in report.failures] == [
        ("identity-2", "RETRIEVAL_IDENTITY_MISMATCH")
    ]
    assert report.gate.decision == GateDecision.FAIL
