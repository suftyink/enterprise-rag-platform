import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from erp_assistant.evaluation.retrieval import (
    GateDecision,
    HttpRetrievalAdapter,
    RetrievalEvaluator,
)
from tests.retrieval_evaluation_support import (
    DeterministicRetriever,
    environment,
    evaluation_case,
    outcome,
    write_dataset,
)


@pytest.mark.asyncio
async def test_run_publishes_reproducible_machine_readable_report_without_sensitive_text(
    tmp_path: Path,
) -> None:
    question = "confidential employee question"
    dataset = write_dataset(
        tmp_path / "reproducible.jsonl",
        [
            evaluation_case(
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
    retriever = DeterministicRetriever({question: outcome("approved-sop")})

    report = await RetrievalEvaluator(retriever, environment()).run(dataset)
    same_report = await RetrievalEvaluator(retriever, environment()).run(dataset)
    changed_report = await RetrievalEvaluator(
        retriever, environment(index_identity="knowledge_chunks_hybrid_v2:2026-09-16")
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
    dataset = write_dataset(
        tmp_path / "http.jsonl",
        [
            evaluation_case(
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
            HttpRetrievalAdapter(client), environment()
        ).run(dataset)

    assert report.metrics.recall_at_5 == 1.0
    assert report.metadata.embedding_model == "embedding-http-v1"
    assert report.metadata.configuration_fingerprint == "b" * 64

    async with httpx.AsyncClient(
        base_url="https://assistant.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    ) as client:
        failed_report = await RetrievalEvaluator(
            HttpRetrievalAdapter(client), environment()
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
        evaluation_case(
            "identity-1",
            "first identity",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["CHINESE_NATURAL_LANGUAGE"],
        ),
        evaluation_case(
            "identity-2",
            "changed identity",
            expected_sources=[{"documentKey": "sop", "pageStart": 1, "pageEnd": 1}],
            expected_decision="MATCHED",
            categories=["ENGLISH_ABBREVIATION"],
        ),
    ]
    original = outcome("sop")
    changed = replace(
        original,
        run=replace(original.run, config_fingerprint="c" * 64),
    )

    report = await RetrievalEvaluator(
        DeterministicRetriever(
            {"first identity": original, "changed identity": changed}
        ),
        environment(),
    ).run(write_dataset(tmp_path / "mixed-identity.jsonl", cases))

    assert report.metrics.evaluated_case_count == 1
    assert [(failure.case_id, failure.code) for failure in report.failures] == [
        ("identity-2", "RETRIEVAL_IDENTITY_MISMATCH")
    ]
    assert report.gate.decision == GateDecision.FAIL
