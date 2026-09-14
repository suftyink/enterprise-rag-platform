from pathlib import Path

import pytest

from erp_assistant.evaluation.retrieval import (
    EvaluationDatasetError,
    GateDecision,
    RetrievalEvaluator,
)
from erp_assistant.rag.types import SearchDecision
from tests.retrieval_evaluation_support import (
    DeterministicRetriever,
    FailIfCalledRetriever,
    PartiallyFailingRetriever,
    environment,
    evaluation_case,
    outcome,
    write_dataset,
)


@pytest.mark.asyncio
async def test_run_evaluates_versioned_dataset_with_retriever_and_reports_metrics(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(
        tmp_path / "evaluation.jsonl",
        [
            evaluation_case(
                "inventory-1",
                "inventory lookup",
                expected_sources=[
                    {"documentKey": "inventory-sop", "pageStart": 1, "pageEnd": 2}
                ],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            ),
            evaluation_case(
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
            "inventory lookup": outcome("inventory-sop", elapsed_ms=10),
            "unknown policy": outcome(
                decision=SearchDecision.INSUFFICIENT_KNOWLEDGE,
                elapsed_ms=20,
            ),
        }
    )

    report = await RetrievalEvaluator(retriever, environment()).run(dataset)

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
        (
            lambda cases: cases[0].update(
                {
                    "expectedSources": [],
                    "expectedDecision": "INSUFFICIENT_KNOWLEDGE",
                    "categories": ["AMBIGUOUS_QUESTION"],
                }
            ),
            "INVALID_CASE_SCHEMA",
        ),
        (
            lambda cases: cases[0].update(
                {"forbiddenDocumentKeys": ["tenant-b-secret"]}
            ),
            "INVALID_CASE_SCHEMA",
        ),
        (
            lambda cases: cases[0].update(
                {
                    "expectedSources": [
                        {"documentKey": "sop", "pageStart": 1, "pageEnd": 2},
                        {"documentKey": "sop", "pageStart": 2, "pageEnd": 3},
                    ]
                }
            ),
            "INVALID_CASE_SCHEMA",
        ),
    ],
)
async def test_run_rejects_invalid_dataset_before_retrieval(
    tmp_path: Path,
    mutate: object,
    error_code: str,
) -> None:
    cases = [
        evaluation_case(
            "case-1",
            "question",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["SKU_PRODUCT_IDENTIFIER"],
        )
    ]
    assert callable(mutate)
    mutate(cases)
    dataset = write_dataset(tmp_path / "invalid.jsonl", cases)

    with pytest.raises(EvaluationDatasetError) as raised:
        await RetrievalEvaluator(FailIfCalledRetriever(), environment()).run(dataset)

    assert raised.value.code == error_code


@pytest.mark.asyncio
async def test_run_reports_dependency_failure_without_counting_it_as_missed_retrieval(
    tmp_path: Path,
) -> None:
    dataset = write_dataset(
        tmp_path / "dependency-failure.jsonl",
        [
            evaluation_case(
                "success-1",
                "successful retrieval",
                expected_sources=[
                    {"documentKey": "inventory-sop", "pageStart": 1, "pageEnd": 1}
                ],
                expected_decision="MATCHED",
                categories=["CHINESE_NATURAL_LANGUAGE"],
            ),
            evaluation_case(
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

    report = await RetrievalEvaluator(PartiallyFailingRetriever(), environment()).run(dataset)

    assert report.metrics.case_count == 2
    assert report.metrics.evaluated_case_count == 1
    assert report.metrics.recall_at_5 == 1.0
    assert [(failure.case_id, failure.code, failure.kind) for failure in report.failures] == [
        ("failure-1", "MILVUS_SEARCH_FAILED", "RETRIEVAL_DEPENDENCY")
    ]
