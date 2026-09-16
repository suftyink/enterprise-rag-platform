import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from redis.exceptions import RedisError

from erp_assistant.core.config import get_settings
from erp_assistant.evaluation.models import (
    AnswerQualityGate,
    AnswerQualityMetrics,
    AnswerQualityReport,
    EvaluationMetadata,
    EvaluationMetrics,
    EvaluationReport,
    EvaluationRoute,
    GateDecision,
    QualityGate,
)
from erp_assistant.infrastructure.database import SessionFactory, engine
from erp_assistant.operations.production_probes import (
    collect_dependencies,
    collect_production_configuration,
)
from erp_assistant.operations.readiness import PilotReadiness
from erp_assistant.operations.readiness_models import (
    CapacityEvidence,
    EvidenceBundle,
    EvidenceCheck,
    ReadinessInputs,
    ReleaseIdentity,
    SecurityEvidence,
)
from erp_assistant.operations.readiness_store import SqlReadinessReportStore
from erp_assistant.operations.telemetry_redis import RedisTelemetryStore
from erp_assistant.orchestration.providers import ProviderBoundary
from erp_assistant.rag.types import DataClassification


async def run_release_check(manifest_path: Path) -> tuple[GateDecision, dict[str, object]]:
    manifest = _object(json.loads(await _read_text(manifest_path)))
    identity = _identity(_object(manifest["release"]))
    settings = get_settings()
    if settings.app_env.casefold() not in {"prod", "production"}:
        raise ValueError("Release checks require a production environment")
    if settings.pilot_release_fingerprint != identity.fingerprint:
        raise ValueError("PILOT_RELEASE_FINGERPRINT does not match the release identity")
    now = datetime.now(UTC)
    configuration, dependencies = await asyncio.gather(
        collect_production_configuration(settings, identity, observed_at=now),
        collect_dependencies(settings, identity, observed_at=now),
    )
    evaluation_path = manifest_path.parent / str(manifest["evaluationReport"])
    evaluation = _evaluation(_object(json.loads(await _read_text(evaluation_path))))
    telemetry_store = RedisTelemetryStore(settings.redis_url)
    try:
        try:
            telemetry = telemetry_store.evidence(
                identity_fingerprint=identity.fingerprint,
                window_end=now,
            )
        except RedisError:
            telemetry = None
    finally:
        telemetry_store.close()
    inputs = ReadinessInputs(
        identity=identity,
        configuration=configuration,
        dependencies=dependencies,
        evaluation=evaluation,
        runtime=telemetry,
        security=_security(_object(manifest["security"])),
        infrastructure=_bundle(_object(manifest["infrastructure"])),
        capacity=_capacity(_object(manifest["capacity"])),
        recovery=_bundle(_object(manifest["recovery"])),
    )
    try:
        report = await PilotReadiness(
            inputs,
            store=SqlReadinessReportStore(SessionFactory),
        ).evaluate()
    finally:
        await engine.dispose()
    return report.decision, report.to_dict()


def _identity(payload: dict[str, Any]) -> ReleaseIdentity:
    return ReleaseIdentity(
        release_version=str(payload["releaseVersion"]),
        dataset_version=str(payload["datasetVersion"]),
        dataset_hash=str(payload["datasetHash"]),
        index_identity=str(payload["indexIdentity"]),
        index_build_hash=str(payload["indexBuildHash"]),
        parser_version=str(payload["parserVersion"]),
        chunk_version=str(payload["chunkVersion"]),
        configuration_fingerprint=str(payload["configurationFingerprint"]),
        route_fingerprints=frozenset(map(str, payload["routeFingerprints"])),
        answer_route_fingerprints=frozenset(map(str, payload["answerRouteFingerprints"])),
    )


def _bundle(payload: dict[str, Any]) -> EvidenceBundle:
    return EvidenceBundle(
        observed_at=_timestamp(payload["observedAt"]),
        identity_fingerprint=str(payload["identityFingerprint"]),
        checks=tuple(
            EvidenceCheck(
                name=str(item["name"]),
                decision=GateDecision(str(item["decision"])),
                reference=str(item["reference"]),
                reason_code=(
                    str(item["reasonCode"]) if item.get("reasonCode") is not None else None
                ),
            )
            for raw in cast(list[object], payload["checks"])
            if (item := _object(raw))
        ),
    )


def _security(payload: dict[str, Any]) -> SecurityEvidence:
    return SecurityEvidence(
        observed_at=_timestamp(payload["observedAt"]),
        identity_fingerprint=str(payload["identityFingerprint"]),
        invalid_citation_count=int(payload["invalidCitationCount"]),
        missing_critical_audit_event_count=int(payload["missingCriticalAuditEventCount"]),
        audit_trace_link_count=int(payload["auditTraceLinkCount"]),
        reference=str(payload["reference"]),
    )


def _capacity(payload: dict[str, Any]) -> CapacityEvidence:
    return CapacityEvidence(
        observed_at=_timestamp(payload["observedAt"]),
        identity_fingerprint=str(payload["identityFingerprint"]),
        reference=str(payload["reference"]),
        throughput_rps=float(payload["throughputRps"]),
        latency_p50_ms=float(payload["latencyP50Ms"]),
        latency_p95_ms=float(payload["latencyP95Ms"]),
        error_rate=float(payload["errorRate"]),
        cpu_high_watermark=float(payload["cpuHighWatermark"]),
        memory_high_watermark=float(payload["memoryHighWatermark"]),
    )


def _evaluation(payload: dict[str, Any]) -> EvaluationReport:
    metadata = _object(payload["metadata"])
    metrics = _object(payload["metrics"])
    gate = _object(payload["gate"])
    answer = _object(payload["answerQuality"])
    answer_metrics = _object(answer["metrics"])
    answer_gate = _object(answer["gate"])
    return EvaluationReport(
        metrics=_metrics(metrics),
        metadata=EvaluationMetadata(
            dataset_version=str(metadata["datasetVersion"]),
            dataset_hash=str(metadata["datasetHash"]),
            index_identity=str(metadata["indexIdentity"]),
            parser_version=str(metadata["parserVersion"]),
            chunk_version=str(metadata["chunkVersion"]),
            retrieval_pipeline=str(metadata["retrievalPipeline"]),
            embedding_model=str(metadata["embeddingModel"]),
            rerank_model=str(metadata["rerankModel"]),
            configuration_fingerprint=str(metadata["configurationFingerprint"]),
            comparison_key=str(metadata["comparisonKey"]),
            executed_at=str(metadata["executedAt"]),
            elapsed_ms=float(metadata["elapsedMs"]),
            index_build_hash=str(metadata["indexBuildHash"]),
            routes=tuple(_route(_object(raw)) for raw in cast(list[object], metadata["routes"])),
        ),
        failures=(),
        category_metrics={},
        gate=QualityGate(
            decision=GateDecision(str(gate["decision"])),
            production_baseline_approved=_strict_bool(gate["productionBaselineApproved"]),
            framework_complete=_strict_bool(gate["frameworkComplete"]),
            reasons=tuple(map(str, gate["reasons"])),
        ),
        answer_quality=AnswerQualityReport(
            dataset_version=str(answer["datasetVersion"]),
            dataset_hash=str(answer["datasetHash"]),
            metrics=AnswerQualityMetrics(
                case_count=int(answer_metrics["caseCount"]),
                citation_support_rate=_optional_float(answer_metrics["citationSupportRate"]),
                answer_refusal_accuracy=_optional_float(answer_metrics["answerRefusalAccuracy"]),
            ),
            gate=AnswerQualityGate(
                decision=GateDecision(str(answer_gate["decision"])),
                reasons=tuple(map(str, answer_gate["reasons"])),
            ),
            route_fingerprints=tuple(map(str, answer["routeFingerprints"])),
        ),
    )


def _metrics(payload: dict[str, Any]) -> EvaluationMetrics:
    return EvaluationMetrics(
        case_count=int(payload["caseCount"]),
        evaluated_case_count=int(payload["evaluatedCaseCount"]),
        recall_at_5=_optional_float(payload["recallAt5"]),
        mrr_at_5=_optional_float(payload["mrrAt5"]),
        ndcg_at_5=_optional_float(payload["ndcgAt5"]),
        refusal_accuracy=_optional_float(payload["refusalAccuracy"]),
        tenant_leakage_count=int(payload["tenantLeakageCount"]),
        latency_p50_ms=float(payload["latencyP50Ms"]),
        latency_p95_ms=float(payload["latencyP95Ms"]),
    )


def _route(payload: dict[str, Any]) -> EvaluationRoute:
    return EvaluationRoute(
        data_classification=DataClassification(str(payload["dataClassification"])),
        embedding_model=str(payload["embeddingModel"]),
        embedding_deployment=str(payload["embeddingDeployment"]),
        embedding_dimension=int(payload["embeddingDimension"]),
        rerank_model=str(payload["rerankModel"]),
        rerank_deployment=str(payload["rerankDeployment"]),
        provider_boundary=ProviderBoundary(str(payload["providerBoundary"])),
        production_ready=_strict_bool(payload["productionReady"]),
        request_count=int(payload["requestCount"]),
        input_tokens=int(payload["inputTokens"]),
        configuration_fingerprint=str(payload["configurationFingerprint"]),
        retrieval_config=cast(dict[str, int | float | bool | str], payload["retrievalConfig"]),
    )


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Release evidence must contain JSON objects")
    return cast(dict[str, Any], value)


def _timestamp(value: object) -> datetime:
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("Evidence timestamps must include a timezone")
    return result


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(str | float | int, value))


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Release evidence boolean fields must use JSON booleans")
    return value


async def _read_text(path: Path) -> str:
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate and persist a pilot release gate")
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    decision, payload = asyncio.run(run_release_check(arguments.manifest))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if decision is GateDecision.PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
