import math

from erp_assistant.evaluation.models import (
    DatasetProvenance,
    EvaluationCase,
    EvaluationCategory,
    EvaluationFailure,
    EvaluationMetrics,
    GateDecision,
    ObservedCase,
    QualityGate,
    RelevantSource,
)
from erp_assistant.rag.retriever import RetrievalEvidence
from erp_assistant.rag.types import SearchDecision


def calculate_metrics(
    observed_cases: list[ObservedCase], case_count: int
) -> EvaluationMetrics:
    expected_sources = 0
    retrieved_sources = 0
    reciprocal_ranks: list[float] = []
    ndcg_scores: list[float] = []
    refusal_total = 0
    refusal_correct = 0
    leakage_count = 0
    for observed in observed_cases:
        evidence = observed.outcome.evidence[:5]
        if observed.case.expected_decision == SearchDecision.INSUFFICIENT_KNOWLEDGE:
            refusal_total += 1
            refusal_correct += int(
                observed.outcome.decision == SearchDecision.INSUFFICIENT_KNOWLEDGE
            )
        if observed.case.expected_sources:
            expected_sources += len(observed.case.expected_sources)
            retrieved_sources += sum(
                any(_is_relevant(item, source) for item in evidence)
                for source in observed.case.expected_sources
            )
            matched_sources: set[RelevantSource] = set()
            relevance: list[int] = []
            for item in evidence:
                matched_source = next(
                    (
                        source
                        for source in observed.case.expected_sources
                        if source not in matched_sources and _is_relevant(item, source)
                    ),
                    None,
                )
                relevance.append(int(matched_source is not None))
                if matched_source is not None:
                    matched_sources.add(matched_source)
            first_rank = next(
                (index for index, value in enumerate(relevance, 1) if value), None
            )
            reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
            dcg = sum(
                value / math.log2(index + 1)
                for index, value in enumerate(relevance, 1)
            )
            ideal_count = min(len(observed.case.expected_sources), 5)
            ideal_dcg = sum(
                1.0 / math.log2(index + 1)
                for index in range(1, ideal_count + 1)
            )
            ndcg_scores.append(0.0 if ideal_dcg == 0 else dcg / ideal_dcg)
        leakage_count += sum(
            item.document_key in observed.case.forbidden_document_keys for item in evidence
        )
    latencies = [item.outcome.run.elapsed_ms for item in observed_cases]
    return EvaluationMetrics(
        case_count=case_count,
        evaluated_case_count=len(observed_cases),
        recall_at_5=(
            round(retrieved_sources / expected_sources, 4) if expected_sources else None
        ),
        mrr_at_5=(
            round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
            if reciprocal_ranks
            else None
        ),
        ndcg_at_5=(
            round(sum(ndcg_scores) / len(ndcg_scores), 4) if ndcg_scores else None
        ),
        refusal_accuracy=(
            round(refusal_correct / refusal_total, 4) if refusal_total else None
        ),
        tenant_leakage_count=leakage_count,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
    )


def build_gate(
    metrics: EvaluationMetrics,
    cases: tuple[EvaluationCase, ...],
    failures: list[EvaluationFailure],
) -> QualityGate:
    failure_reasons: list[str] = []
    if failures:
        failure_reasons.append("EXECUTION_FAILURES")
    if metrics.tenant_leakage_count != 0:
        failure_reasons.append("TENANT_LEAKAGE")
    if metrics.recall_at_5 is not None and metrics.recall_at_5 < 0.90:
        failure_reasons.append("RECALL_AT_5_BELOW_THRESHOLD")
    if metrics.mrr_at_5 is not None and metrics.mrr_at_5 < 0.80:
        failure_reasons.append("MRR_AT_5_BELOW_THRESHOLD")
    if metrics.refusal_accuracy is not None and metrics.refusal_accuracy < 0.90:
        failure_reasons.append("REFUSAL_ACCURACY_BELOW_THRESHOLD")
    if failure_reasons:
        return QualityGate(
            decision=GateDecision.FAIL,
            production_baseline_approved=False,
            framework_complete=True,
            reasons=tuple(failure_reasons),
        )

    indeterminate_reasons: list[str] = []
    present_categories = {category for case in cases for category in case.categories}
    if present_categories != set(EvaluationCategory):
        indeterminate_reasons.append("REQUIRED_CATEGORIES_INCOMPLETE")
    if len(cases) < 100:
        indeterminate_reasons.append("PRODUCTION_CASE_COUNT_TOO_LOW")
    if any(case.provenance != DatasetProvenance.BUSINESS for case in cases):
        indeterminate_reasons.append("NON_BUSINESS_CASES")
    if any(not case.reviewed for case in cases):
        indeterminate_reasons.append("UNREVIEWED_CASES")
    if (
        metrics.recall_at_5 is None
        or metrics.mrr_at_5 is None
        or metrics.refusal_accuracy is None
    ):
        indeterminate_reasons.append("REQUIRED_METRICS_UNAVAILABLE")
    if indeterminate_reasons:
        return QualityGate(
            decision=GateDecision.INDETERMINATE,
            production_baseline_approved=False,
            framework_complete=True,
            reasons=tuple(indeterminate_reasons),
        )
    return QualityGate(
        decision=GateDecision.PASS,
        production_baseline_approved=True,
        framework_complete=True,
        reasons=(),
    )


def _is_relevant(evidence: RetrievalEvidence, source: RelevantSource) -> bool:
    return (
        evidence.document_key == source.document_key
        and evidence.page_start <= source.page_end
        and evidence.page_end >= source.page_start
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)
