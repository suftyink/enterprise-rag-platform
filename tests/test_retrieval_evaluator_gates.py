from pathlib import Path

import pytest

from erp_assistant.evaluation.retrieval import GateDecision, RetrievalEvaluator
from erp_assistant.rag.retriever import RetrievalOutcome
from erp_assistant.rag.types import SearchDecision
from tests.retrieval_evaluation_support import (
    DeterministicRetriever,
    environment,
    evaluation_case,
    outcome,
    write_dataset,
)


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
            outcomes[question] = outcome(decision=SearchDecision.INSUFFICIENT_KNOWLEDGE)
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
            outcomes[question] = outcome("approved-sop")
    dataset = write_dataset(tmp_path / "production-baseline.jsonl", cases)

    report = await RetrievalEvaluator(
        DeterministicRetriever(outcomes), environment()
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
        evaluation_case(
            "miss-1",
            "missed evidence",
            expected_sources=[{"documentKey": "expected-sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["CHINESE_NATURAL_LANGUAGE"],
        ),
        evaluation_case(
            "refusal-1",
            "unsupported question",
            expected_sources=[],
            expected_decision="INSUFFICIENT_KNOWLEDGE",
            categories=["NO_ANSWER"],
        ),
        evaluation_case(
            "leak-1",
            "tenant probe",
            expected_sources=[{"documentKey": "approved-sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["TENANT_ISOLATION"],
            forbidden_document_keys=["tenant-b-secret"],
        ),
    ]
    outcomes = {
        "missed evidence": outcome(
            decision=SearchDecision.INSUFFICIENT_KNOWLEDGE
        ),
        "unsupported question": outcome("unrelated-doc"),
        "tenant probe": outcome("tenant-b-secret"),
    }

    report = await RetrievalEvaluator(
        DeterministicRetriever(outcomes), environment()
    ).run(write_dataset(tmp_path / "failed-baseline.jsonl", cases))

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
    dataset = write_dataset(
        tmp_path / "duplicate-chunks.jsonl",
        [
            evaluation_case(
                "duplicate-chunks-1",
                "overlapping chunks",
                expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
                expected_decision="MATCHED",
                categories=["NUMERIC_TABLE_FACT"],
            )
        ],
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever({"overlapping chunks": outcome("sop", "sop")}),
        environment(),
    ).run(dataset)

    assert report.metrics.recall_at_5 == 1.0
    assert report.metrics.ndcg_at_5 == 1.0

