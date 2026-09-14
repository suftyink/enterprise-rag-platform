import json
from pathlib import Path

from erp_assistant.evaluation.retrieval import CaseResult, calculate_metrics, load_cases


def test_load_cases_and_calculate_metrics(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "inventory-1",
                        "tenantId": "tenant-a",
                        "query": "inventory",
                        "relevantSources": [
                            {"documentKey": "inventory-sop", "pageStart": 2, "pageEnd": 3}
                        ],
                        "shouldRefuse": False,
                        "forbiddenDocumentKeys": ["tenant-b-secret"],
                    }
                ),
                json.dumps(
                    {
                        "id": "refuse-1",
                        "tenantId": "tenant-a",
                        "query": "unknown",
                        "relevantSources": [],
                        "shouldRefuse": True,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    cases = load_cases(dataset)
    results = [
        CaseResult(
            case=cases[0],
            decision="MATCHED",
            results=[
                {
                    "documentKey": "inventory-sop",
                    "pageStart": 3,
                    "pageEnd": 3,
                }
            ],
            latency_ms=10,
        ),
        CaseResult(
            case=cases[1],
            decision="INSUFFICIENT_KNOWLEDGE",
            results=[],
            latency_ms=20,
        ),
    ]

    metrics = calculate_metrics(results)

    assert metrics["recallAt5"] == 1.0
    assert metrics["mrrAt5"] == 1.0
    assert metrics["ndcgAt5"] == 1.0
    assert metrics["refusalAccuracy"] == 1.0
    assert metrics["tenantLeakageCount"] == 0
    assert metrics["latencyP50Ms"] == 10
    assert metrics["latencyP95Ms"] == 20
