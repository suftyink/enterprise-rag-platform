import argparse
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import UUID

import httpx

from erp_assistant.core.config import get_settings
from erp_assistant.evaluation.dataset import load_dataset
from erp_assistant.evaluation.http_adapter import HttpRetrievalAdapter
from erp_assistant.evaluation.metrics import build_gate, calculate_metrics
from erp_assistant.evaluation.models import (
    EvaluationActivation,
    EvaluationCategory,
    EvaluationDatasetError,
    EvaluationEnvironment,
    EvaluationFailure,
    EvaluationFailureKind,
    EvaluationMetadata,
    EvaluationReport,
    EvaluationRoute,
    GateDecision,
    ObservedCase,
    Retriever,
)
from erp_assistant.evaluation.profiles import ProfileActivator
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.orchestration.provider_factory import (
    get_embedding_provider,
    get_rerank_provider,
)
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.retriever import KnowledgeRetriever, RetrievalRequest, RetrievalRun
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import get_vector_store

__all__ = [
    "EvaluationDatasetError",
    "EvaluationEnvironment",
    "GateDecision",
    "HttpRetrievalAdapter",
    "RetrievalEvaluator",
]


class RetrievalEvaluator:
    def __init__(
        self,
        retriever: Retriever,
        environment: EvaluationEnvironment,
        *,
        activator: ProfileActivator | None = None,
        candidate_profile_id: UUID | None = None,
    ) -> None:
        if (activator is None) != (candidate_profile_id is None):
            raise ValueError("Activator and candidate profile ID must be supplied together")
        self._retriever = retriever
        self._environment = environment
        self._activator = activator
        self._candidate_profile_id = candidate_profile_id

    async def run(self, dataset_path: Path) -> EvaluationReport:
        executed_at = datetime.now(UTC).isoformat()
        started = perf_counter()
        dataset = load_dataset(dataset_path)
        observed: list[ObservedCase] = []
        failures: list[EvaluationFailure] = []
        run_identities: dict[DataClassification, RetrievalRun] = {}

        for case in dataset.cases:
            try:
                outcome = await self._retriever.retrieve(
                    RetrievalRequest(
                        query=case.question,
                        actor=case.actor,
                        evidence_limit=5,
                        data_classification=case.data_classification,
                    )
                )
            except RagDependencyError as exc:
                failures.append(
                    EvaluationFailure(
                        case_id=case.case_id,
                        code=exc.code,
                        kind=(
                            EvaluationFailureKind.DEPLOYMENT_EXECUTION
                            if exc.code.startswith(("HTTP_", "PROVIDER_"))
                            else EvaluationFailureKind.RETRIEVAL_DEPENDENCY
                        ),
                    )
                )
                continue
            run_identity = run_identities.get(case.data_classification)
            if run_identity is not None and _identity(outcome.run) != _identity(run_identity):
                failures.append(
                    EvaluationFailure(
                        case_id=case.case_id,
                        code="RETRIEVAL_IDENTITY_MISMATCH",
                        kind=EvaluationFailureKind.RETRIEVAL_CONFIGURATION,
                    )
                )
                continue
            observed.append(ObservedCase(case=case, outcome=outcome))
            if run_identity is None:
                run_identities[case.data_classification] = outcome.run

        for classification, run in run_identities.items():
            if not run.production_ready:
                failures.append(
                    EvaluationFailure(
                        case_id=f"route:{classification.value}",
                        code="UNAPPROVED_PRODUCTION_PROFILE",
                        kind=EvaluationFailureKind.RETRIEVAL_CONFIGURATION,
                    )
                )

        metrics = calculate_metrics(observed, len(dataset.cases))
        category_metrics = {
            category.value: calculate_metrics(
                [item for item in observed if category in item.case.categories],
                sum(category in case.categories for case in dataset.cases),
            )
            for category in EvaluationCategory
        }
        gate = build_gate(metrics, dataset.cases, failures, self._environment.thresholds)
        primary_run = next(iter(run_identities.values()), None)
        comparison_key = _comparison_key(
            dataset_version=dataset.version,
            dataset_hash=dataset.content_hash,
            environment=self._environment,
            runs=run_identities,
        )
        report = EvaluationReport(
            metrics=metrics,
            metadata=EvaluationMetadata(
                dataset_version=dataset.version,
                dataset_hash=dataset.content_hash,
                index_identity=self._environment.index_identity,
                parser_version=self._environment.parser_version,
                chunk_version=self._environment.chunk_version,
                retrieval_pipeline=primary_run.retrieval if primary_run else None,
                embedding_model=primary_run.embedding_model if primary_run else None,
                rerank_model=primary_run.rerank_model if primary_run else None,
                configuration_fingerprint=(primary_run.config_fingerprint if primary_run else None),
                comparison_key=comparison_key,
                executed_at=executed_at,
                elapsed_ms=round((perf_counter() - started) * 1000, 2),
                routes=tuple(
                    EvaluationRoute(
                        data_classification=classification,
                        embedding_model=run.embedding_model,
                        embedding_deployment=run.embedding_deployment,
                        embedding_dimension=run.embedding_dimension,
                        rerank_model=run.rerank_model,
                        rerank_deployment=run.rerank_deployment,
                        provider_boundary=run.provider_boundary,
                        production_ready=run.production_ready,
                        request_count=run.request_count,
                        input_tokens=run.input_tokens,
                        configuration_fingerprint=run.config_fingerprint,
                    )
                    for classification, run in sorted(
                        run_identities.items(), key=lambda item: item[0].value
                    )
                ),
            ),
            failures=tuple(failures),
            category_metrics=category_metrics,
            gate=gate,
        )
        if (
            report.gate.decision is GateDecision.PASS
            and self._activator is not None
            and self._candidate_profile_id is not None
        ):
            try:
                activated = await self._activator.activate(self._candidate_profile_id, report)
            except RagError as exc:
                activation_failure = EvaluationFailure(
                    case_id=f"profile:{self._candidate_profile_id}",
                    code=exc.code,
                    kind=EvaluationFailureKind.RETRIEVAL_CONFIGURATION,
                )
                updated_failures = [*report.failures, activation_failure]
                report = replace(
                    report,
                    failures=tuple(updated_failures),
                    gate=build_gate(
                        report.metrics,
                        dataset.cases,
                        updated_failures,
                        self._environment.thresholds,
                    ),
                )
            else:
                report = replace(
                    report,
                    activation=EvaluationActivation(
                        profile_id=str(activated.profile_id),
                        previous_profile_id=(
                            str(activated.previous_profile_id)
                            if activated.previous_profile_id
                            else None
                        ),
                        status=activated.status.value,
                    ),
                )
        return report


def _identity(run: RetrievalRun) -> tuple[object, ...]:
    return (
        run.retrieval,
        run.embedding_model,
        run.rerank_model,
        run.threshold,
        run.config_fingerprint,
        run.embedding_deployment,
        run.embedding_dimension,
        run.rerank_deployment,
        run.provider_boundary,
        run.production_ready,
    )


def _comparison_key(
    *,
    dataset_version: str,
    dataset_hash: str,
    environment: EvaluationEnvironment,
    runs: dict[DataClassification, RetrievalRun],
) -> str:
    payload = json.dumps(
        {
            "chunkVersion": environment.chunk_version,
            "datasetHash": dataset_hash,
            "datasetVersion": dataset_version,
            "indexIdentity": environment.index_identity,
            "parserVersion": environment.parser_version,
            "thresholds": {
                "latencyP95Ms": environment.thresholds.latency_p95_ms,
                "maximumTenantLeakage": environment.thresholds.maximum_tenant_leakage,
                "mrrAt5": environment.thresholds.mrr_at_5,
                "recallAt5": environment.thresholds.recall_at_5,
                "refusalAccuracy": environment.thresholds.refusal_accuracy,
            },
            "routes": {
                classification.value: {
                    "configurationFingerprint": run.config_fingerprint,
                    "embeddingDeployment": run.embedding_deployment,
                    "embeddingDimension": run.embedding_dimension,
                    "embeddingModel": run.embedding_model,
                    "providerBoundary": run.provider_boundary.value,
                    "rerankDeployment": run.rerank_deployment,
                    "rerankModel": run.rerank_model,
                    "retrievalPipeline": run.retrieval,
                }
                for classification, run in sorted(runs.items(), key=lambda item: item[0].value)
            },
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
            return await RetrievalEvaluator(HttpRetrievalAdapter(client), environment).run(
                args.dataset
            )
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
