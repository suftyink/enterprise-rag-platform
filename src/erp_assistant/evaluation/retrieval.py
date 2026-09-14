import argparse
import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx


@dataclass(frozen=True)
class RelevantSource:
    document_key: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    tenant_id: str
    operator_id: str
    query: str
    relevant_sources: list[RelevantSource]
    should_refuse: bool
    forbidden_document_keys: set[str]


@dataclass(frozen=True)
class CaseResult:
    case: EvaluationCase
    decision: str
    results: list[dict[str, Any]]
    latency_ms: float


def load_cases(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                sources = [
                    RelevantSource(
                        document_key=item["documentKey"],
                        page_start=int(item["pageStart"]),
                        page_end=int(item["pageEnd"]),
                    )
                    for item in value.get("relevantSources", [])
                ]
                cases.append(
                    EvaluationCase(
                        case_id=value["id"],
                        tenant_id=value["tenantId"],
                        operator_id=value.get("operatorId", "evaluation"),
                        query=value["query"],
                        relevant_sources=sources,
                        should_refuse=bool(value.get("shouldRefuse", False)),
                        forbidden_document_keys=set(value.get("forbiddenDocumentKeys", [])),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid evaluation case at line {line_number}") from exc
    return cases


def _is_relevant(result: dict[str, Any], source: RelevantSource) -> bool:
    return bool(
        result.get("documentKey") == source.document_key
        and int(result.get("pageStart", 0)) <= source.page_end
        and int(result.get("pageEnd", 0)) >= source.page_start
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def calculate_metrics(case_results: list[CaseResult], top_k: int = 5) -> dict[str, float | int]:
    expected_sources = 0
    retrieved_sources = 0
    reciprocal_ranks: list[float] = []
    ndcg_scores: list[float] = []
    refusal_total = 0
    refusal_correct = 0
    leakage_count = 0

    for observed in case_results:
        results = observed.results[:top_k]
        if observed.case.should_refuse:
            refusal_total += 1
            refusal_correct += int(observed.decision == "INSUFFICIENT_KNOWLEDGE")
        if observed.case.relevant_sources:
            expected_sources += len(observed.case.relevant_sources)
            retrieved_sources += sum(
                any(_is_relevant(result, source) for result in results)
                for source in observed.case.relevant_sources
            )
            relevance = [
                int(any(_is_relevant(result, source) for source in observed.case.relevant_sources))
                for result in results
            ]
            first_rank = next((index for index, value in enumerate(relevance, 1) if value), None)
            reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
            dcg = sum(value / math.log2(index + 1) for index, value in enumerate(relevance, 1))
            ideal_count = min(len(observed.case.relevant_sources), top_k)
            ideal_dcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
            ndcg_scores.append(0.0 if ideal_dcg == 0 else dcg / ideal_dcg)
        leakage_count += sum(
            result.get("documentKey") in observed.case.forbidden_document_keys
            for result in results
        )

    latencies = [item.latency_ms for item in case_results]
    return {
        "caseCount": len(case_results),
        "recallAt5": round(retrieved_sources / expected_sources, 4)
        if expected_sources
        else 0.0,
        "mrrAt5": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
        if reciprocal_ranks
        else 0.0,
        "ndcgAt5": round(sum(ndcg_scores) / len(ndcg_scores), 4)
        if ndcg_scores
        else 0.0,
        "refusalAccuracy": round(refusal_correct / refusal_total, 4)
        if refusal_total
        else 0.0,
        "tenantLeakageCount": leakage_count,
        "latencyP50Ms": _percentile(latencies, 0.50),
        "latencyP95Ms": _percentile(latencies, 0.95),
    }


async def run_evaluation(
    cases: list[EvaluationCase], base_url: str, top_k: int
) -> list[CaseResult]:
    observed: list[CaseResult] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        for case in cases:
            started = perf_counter()
            response = await client.post(
                "/api/assistant/v1/knowledge/search",
                json={
                    "tenantId": case.tenant_id,
                    "operatorId": case.operator_id,
                    "query": case.query,
                    "topK": top_k,
                },
            )
            latency_ms = (perf_counter() - started) * 1000
            response.raise_for_status()
            payload = response.json()
            observed.append(
                CaseResult(
                    case=case,
                    decision=payload["decision"],
                    results=payload["results"],
                    latency_ms=latency_ms,
                )
            )
    return observed


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate knowledge retrieval through HTTP API")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--top-k", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    results = await run_evaluation(load_cases(args.dataset), args.base_url, args.top_k)
    print(json.dumps(calculate_metrics(results, args.top_k), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
