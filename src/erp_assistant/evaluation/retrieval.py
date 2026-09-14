import argparse
import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx

from erp_assistant.core.config import get_settings
from erp_assistant.evaluation.dataset import load_dataset
from erp_assistant.evaluation.http_adapter import HttpRetrievalAdapter
from erp_assistant.evaluation.metrics import build_gate, calculate_metrics
from erp_assistant.evaluation.models import (
    EvaluationCategory,
    EvaluationDatasetError,
    EvaluationEnvironment,
    EvaluationFailure,
    EvaluationMetadata,
    EvaluationReport,
    GateDecision,
    ObservedCase,
    Retriever,
)
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.orchestration.provider_factory import (
    get_embedding_provider,
    get_rerank_provider,
)
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import KnowledgeRetriever, RetrievalRequest, RetrievalRun
from erp_assistant.rag.vector_store import get_vector_store

__all__ = [
    "EvaluationDatasetError",
    "EvaluationEnvironment",
    "GateDecision",
    "HttpRetrievalAdapter",
    "RetrievalEvaluator",
]


class RetrievalEvaluator:
    def __init__(self, retriever: Retriever, environment: EvaluationEnvironment) -> None:
        self._retriever = retriever
        self._environment = environment

    async def run(self, dataset_path: Path) -> EvaluationReport:
        executed_at = datetime.now(UTC).isoformat()
        started = perf_counter()
        dataset = load_dataset(dataset_path)
        observed: list[ObservedCase] = []
        failures: list[EvaluationFailure] = []
        run_identity: RetrievalRun | None = None

        for case in dataset.cases:
            try:
                outcome = await self._retriever.retrieve(
                    RetrievalRequest(
                        query=case.question,
                        actor=case.actor,
                        evidence_limit=5,
                    )
                )
            except RagDependencyError as exc:
                failures.append(
                    EvaluationFailure(
                        case_id=case.case_id,
                        code=exc.code,
                        kind=(
                            "DEPLOYMENT_EXECUTION"
                            if exc.code.startswith("HTTP_")
                            else "RETRIEVAL_DEPENDENCY"
                        ),
                    )
                )
                continue
            if run_identity is not None and _identity(outcome.run) != _identity(run_identity):
                failures.append(
                    EvaluationFailure(
                        case_id=case.case_id,
                        code="RETRIEVAL_IDENTITY_MISMATCH",
                        kind="RETRIEVAL_CONFIGURATION",
                    )
                )
                continue
            observed.append(ObservedCase(case=case, outcome=outcome))
            if run_identity is None:
                run_identity = outcome.run

        metrics = calculate_metrics(observed, len(dataset.cases))
        category_metrics = {
            category.value: calculate_metrics(
                [item for item in observed if category in item.case.categories],
                sum(category in case.categories for case in dataset.cases),
            )
            for category in EvaluationCategory
        }
        gate = build_gate(metrics, dataset.cases, failures)
        comparison_key = _comparison_key(
            dataset_version=dataset.version,
            dataset_hash=dataset.content_hash,
            environment=self._environment,
            run=run_identity,
        )
        return EvaluationReport(
            metrics=metrics,
            metadata=EvaluationMetadata(
                dataset_version=dataset.version,
                dataset_hash=dataset.content_hash,
                index_identity=self._environment.index_identity,
                parser_version=self._environment.parser_version,
                chunk_version=self._environment.chunk_version,
                retrieval_pipeline=run_identity.retrieval if run_identity else None,
                embedding_model=run_identity.embedding_model if run_identity else None,
                rerank_model=run_identity.rerank_model if run_identity else None,
                configuration_fingerprint=(
                    run_identity.config_fingerprint if run_identity else None
                ),
                comparison_key=comparison_key,
                executed_at=executed_at,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
            ),
            failures=tuple(failures),
            category_metrics=category_metrics,
            gate=gate,
        )


def _identity(run: RetrievalRun) -> tuple[str, str, str, float, str]:
    return (
        run.retrieval,
        run.embedding_model,
        run.rerank_model,
        run.threshold,
        run.config_fingerprint,
    )


def _comparison_key(
    *,
    dataset_version: str,
    dataset_hash: str,
    environment: EvaluationEnvironment,
    run: RetrievalRun | None,
) -> str:
    payload = json.dumps(
        {
            "chunkVersion": environment.chunk_version,
            "configurationFingerprint": run.config_fingerprint if run else None,
            "datasetHash": dataset_hash,
            "datasetVersion": dataset_version,
            "embeddingModel": run.embedding_model if run else None,
            "indexIdentity": environment.index_identity,
            "parserVersion": environment.parser_version,
            "rerankModel": run.rerank_model if run else None,
            "retrievalPipeline": run.retrieval if run else None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


async def _run_from_cli(args: argparse.Namespace) -> EvaluationReport:
    settings = get_settings()
    environment = EvaluationEnvironment(
        index_identity=args.index_identity or settings.milvus_collection,
        parser_version=args.parser_version,
        chunk_version=(
            args.chunk_version
            or (
                f"page-aware-v1:{settings.knowledge_chunk_target}:"
                f"{settings.knowledge_chunk_max}:{settings.knowledge_chunk_overlap}"
            )
        ),
    )
    if args.base_url:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
            return await RetrievalEvaluator(
                HttpRetrievalAdapter(client), environment
            ).run(args.dataset)
    async with SessionFactory() as session:
        retriever = KnowledgeRetriever(
            session=session,
            vector_store=get_vector_store(),
            embedding=get_embedding_provider(),
            reranker=get_rerank_provider(),
            settings=settings,
        )
        return await RetrievalEvaluator(retriever, environment).run(args.dataset)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the retrieval quality baseline")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--index-identity")
    parser.add_argument("--parser-version", default="pdf-parser-v1")
    parser.add_argument("--chunk-version")
    args = parser.parse_args()
    report = await _run_from_cli(args)
    if args.report:
        report.write(args.report)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
