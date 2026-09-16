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
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import get_settings
from erp_assistant.evaluation.answer_quality import AnswerQualityEvaluator
from erp_assistant.evaluation.dataset import load_dataset
from erp_assistant.evaluation.http_adapter import HttpRetrievalAdapter
from erp_assistant.evaluation.metrics import build_gate, calculate_metrics
from erp_assistant.evaluation.models import (
    AnswerQualityGate,
    EvaluationActivation,
    EvaluationCategory,
    EvaluationDataset,
    EvaluationDatasetError,
    EvaluationEnvironment,
    EvaluationFailure,
    EvaluationFailureKind,
    EvaluationMetadata,
    EvaluationReport,
    EvaluationRoute,
    GateDecision,
    ObservedCase,
    QualityGate,
    Retriever,
)
from erp_assistant.evaluation.profiles import ProfileActivator, SqlRetrievalProfileStore
from erp_assistant.identity import IdentityAccess, IdentityMembership
from erp_assistant.identity.audit import SqlSecurityAuditSink
from erp_assistant.identity.composition import DeterministicIdentityVerifier
from erp_assistant.identity.directory import StaticIdentityDirectory
from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.orchestration.provider_factory import (
    get_chat_provider_for,
    get_embedding_provider_for,
    get_rerank_provider_for,
)
from erp_assistant.rag.classified_retriever import ClassifiedRetrieverRouter
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.knowledge_assistant import KnowledgeAssistant
from erp_assistant.rag.retriever import KnowledgeRetriever, RetrievalRequest, RetrievalRun
from erp_assistant.rag.runtime_composition import get_retrieval_runtime_registry
from erp_assistant.rag.runtime_registry import RetrievalRuntimeRegistry
from erp_assistant.rag.staging_index import build_staging_index
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import get_vector_store_for

__all__ = [
    "EvaluationDatasetError",
    "EvaluationEnvironment",
    "GateDecision",
    "HttpRetrievalAdapter",
    "RetrievalEvaluator",
    "evaluate_candidate_profile",
]


class RetrievalEvaluator:
    def __init__(
        self,
        retriever: Retriever,
        environment: EvaluationEnvironment,
        *,
        activator: ProfileActivator | None = None,
        candidate_profile_id: UUID | None = None,
        answer_evaluator: AnswerQualityEvaluator | None = None,
    ) -> None:
        if (activator is None) != (candidate_profile_id is None):
            raise ValueError("Activator and candidate profile ID must be supplied together")
        self._retriever = retriever
        self._environment = environment
        self._activator = activator
        self._candidate_profile_id = candidate_profile_id
        self._answer_evaluator = answer_evaluator

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
            else:
                run_identities[case.data_classification] = replace(
                    run_identity,
                    request_count=run_identity.request_count + outcome.run.request_count,
                    input_tokens=run_identity.input_tokens + outcome.run.input_tokens,
                )

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
                index_build_hash=self._environment.index_build_hash,
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
                        retrieval_config=dict(run.retrieval_config),
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
        if self._answer_evaluator is not None:
            answer_quality = await self._answer_evaluator.run(dataset)
            report = replace(
                report,
                answer_quality=answer_quality,
                gate=_merge_answer_quality_gate(report.gate, answer_quality.gate),
            )
        elif self._activator is not None and report.gate.decision is GateDecision.PASS:
            missing_gate = EvaluationFailure(
                case_id=f"profile:{self._candidate_profile_id}",
                code="ANSWER_QUALITY_GATE_MISSING",
                kind=EvaluationFailureKind.RETRIEVAL_CONFIGURATION,
            )
            updated_failures = [*report.failures, missing_gate]
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
        tuple(sorted(run.retrieval_config.items())),
    )


def _merge_answer_quality_gate(
    retrieval_gate: QualityGate,
    answer_gate: AnswerQualityGate,
) -> QualityGate:
    answer_decision = answer_gate.decision
    answer_reasons = tuple(f"ANSWER_QUALITY_{reason}" for reason in answer_gate.reasons)
    decision = retrieval_gate.decision
    if answer_decision is GateDecision.FAIL:
        decision = GateDecision.FAIL
    elif answer_decision is GateDecision.INDETERMINATE and decision is GateDecision.PASS:
        decision = GateDecision.INDETERMINATE
    return QualityGate(
        decision=decision,
        production_baseline_approved=(
            retrieval_gate.production_baseline_approved and answer_decision is GateDecision.PASS
        ),
        framework_complete=retrieval_gate.framework_complete,
        reasons=(*retrieval_gate.reasons, *answer_reasons),
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
            "indexBuildHash": environment.index_build_hash,
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
                    "retrievalConfig": run.retrieval_config,
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
    candidate_profile_id = getattr(args, "candidate_profile_id", None)
    if candidate_profile_id is not None:
        if args.base_url:
            raise ValueError("Candidate profile activation is only available in local mode")
        registry = get_retrieval_runtime_registry()
        if getattr(args, "build_staging_index", False):
            await build_staging_index(candidate_profile_id, registry)
        return await evaluate_candidate_profile(
            args.dataset,
            candidate_profile_id,
            registry,
        )
    index_identity = args.index_identity or settings.milvus_collection
    vector_stores = None
    classifications: set[DataClassification] = set()
    dataset: EvaluationDataset | None = None
    if not args.base_url:
        dataset = load_dataset(args.dataset)
        classifications = {case.data_classification for case in dataset.cases}
        vector_stores = {
            classification: get_vector_store_for(classification)
            for classification in classifications
        }
        if not args.index_identity:
            index_identity = "|".join(
                vector_stores[classification].identity
                for classification in sorted(classifications, key=lambda item: item.value)
            )
    environment = EvaluationEnvironment(
        index_identity=index_identity,
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
        credential = settings.evaluation_bearer_token.get_secret_value()
        headers = {"Authorization": f"Bearer {credential}"} if credential else None
        async with httpx.AsyncClient(
            base_url=args.base_url,
            timeout=30.0,
            headers=headers,
        ) as client:
            return await RetrievalEvaluator(HttpRetrievalAdapter(client), environment).run(
                args.dataset
            )
    async with SessionFactory() as session:
        assert dataset is not None
        assert vector_stores is not None
        identity_access = _evaluation_identity_access(dataset, session)
        retriever = ClassifiedRetrieverRouter(
            {
                classification: KnowledgeRetriever(
                    session=session,
                    vector_store=vector_stores[classification],
                    embedding=get_embedding_provider_for(classification),
                    reranker=get_rerank_provider_for(classification),
                    settings=settings,
                    identity_access=identity_access,
                )
                for classification in classifications
            }
        )
        return await RetrievalEvaluator(retriever, environment).run(args.dataset)


async def evaluate_candidate_profile(
    dataset: Path,
    profile_id: UUID,
    registry: RetrievalRuntimeRegistry,
) -> EvaluationReport:
    loaded_dataset = load_dataset(dataset)
    async with SessionFactory() as session:
        identity_access = _evaluation_identity_access(loaded_dataset, session)
        candidate = await session.get(RetrievalProfileRecord, profile_id)
        if candidate is None:
            raise RagError("PROFILE_NOT_FOUND", "staging Profile 不存在")
        try:
            classification = DataClassification(candidate.data_classification)
        except ValueError as exc:
            raise RagError("INVALID_RETRIEVAL_PROFILE", "检索 Profile 数据分类无效") from exc
        retriever = ClassifiedRetrieverRouter(
            {classification: registry.resolve(candidate, session, identity_access)}
        )
        environment = EvaluationEnvironment(
            index_identity=candidate.index_identity,
            parser_version=candidate.parser_version,
            chunk_version=candidate.chunk_version,
            index_build_hash=candidate.index_build_hash,
        )
        activator = SqlRetrievalProfileStore(
            SessionFactory,
            runtime_binding_validator=registry.can_resolve,
        )
        assistant = KnowledgeAssistant(
            retriever=retriever,
            chat_routes={classification: get_chat_provider_for(classification)},
            session_factory=SessionFactory,
            identity_access=identity_access,
            context_token_budget=get_settings().answer_context_token_budget,
        )
        return await RetrievalEvaluator(
            retriever,
            environment,
            activator=activator,
            candidate_profile_id=profile_id,
            answer_evaluator=AnswerQualityEvaluator(assistant),
        ).run(dataset)


def _evaluation_identity_access(
    dataset: EvaluationDataset,
    session: AsyncSession,
) -> IdentityAccess:
    memberships = {
        case.actor.subject_id: IdentityMembership(
            tenant_id=case.actor.tenant_id,
            roles=case.actor.roles,
            knowledge_scopes=case.actor.knowledge_scopes,
        )
        for case in dataset.cases
    }
    return IdentityAccess(
        verifier=DeterministicIdentityVerifier(),
        directory=StaticIdentityDirectory(memberships),
        audit=SqlSecurityAuditSink(session),
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the retrieval quality baseline")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--index-identity")
    parser.add_argument("--parser-version", default="pdf-parser-v1")
    parser.add_argument("--chunk-version")
    parser.add_argument("--candidate-profile-id", type=UUID)
    parser.add_argument("--build-staging-index", action="store_true")
    args = parser.parse_args()
    if args.build_staging_index and args.candidate_profile_id is None:
        parser.error("--build-staging-index requires --candidate-profile-id")
    report = await _run_from_cli(args)
    if args.report:
        report.write(args.report)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
