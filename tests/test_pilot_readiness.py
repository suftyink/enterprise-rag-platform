from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings
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
from erp_assistant.models.knowledge import KnowledgeIndexJob
from erp_assistant.operations import production_probes, release_check
from erp_assistant.operations.evidence import (
    ProductionConfigurationProbe,
    production_configuration_evidence,
    validate_production_startup,
)
from erp_assistant.operations.queue_monitor import SqlIndexQueueMonitor
from erp_assistant.operations.readiness import PilotReadiness
from erp_assistant.operations.readiness_models import (
    CapacityEvidence,
    EvidenceBundle,
    EvidenceCheck,
    PilotPolicy,
    ReadinessInputs,
    ReleaseIdentity,
    RuntimeEvidence,
    SecurityEvidence,
)
from erp_assistant.operations.readiness_store import SqlReadinessReportStore
from erp_assistant.operations.telemetry import (
    OperationalTelemetry,
    RecordingTelemetryExporter,
    runtime_evidence_from_events,
)
from erp_assistant.operations.telemetry_redis import RedisTelemetryStore
from erp_assistant.orchestration.providers import ProviderBoundary
from erp_assistant.rag.types import DataClassification

NOW = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
IDENTITY = ReleaseIdentity(
    release_version="0.1.0+spec09",
    dataset_version="pilot-v1",
    dataset_hash="dataset-hash",
    index_identity="milvus-v3:pilot",
    index_build_hash="index-build-hash",
    parser_version="parser-v1",
    chunk_version="chunk-v1",
    configuration_fingerprint="config-v1",
    route_fingerprints=frozenset({"ORDINARY:ordinary-route-v1"}),
    answer_route_fingerprints=frozenset({"ORDINARY:ordinary-answer-route-v1"}),
)


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    from erp_assistant.infrastructure.database import Base

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _checks(names: frozenset[str]) -> tuple[EvidenceCheck, ...]:
    return tuple(
        EvidenceCheck(name=name, decision=GateDecision.PASS, reference=f"run://{name}")
        for name in sorted(names)
    )


def _bundle(names: frozenset[str]) -> EvidenceBundle:
    return EvidenceBundle(
        observed_at=NOW,
        identity_fingerprint=IDENTITY.fingerprint,
        checks=_checks(names),
    )


def _evaluation() -> EvaluationReport:
    metrics = EvaluationMetrics(
        case_count=20,
        evaluated_case_count=20,
        recall_at_5=0.96,
        mrr_at_5=0.91,
        ndcg_at_5=0.92,
        refusal_accuracy=0.94,
        tenant_leakage_count=0,
        latency_p50_ms=120.0,
        latency_p95_ms=420.0,
    )
    return EvaluationReport(
        metrics=metrics,
        metadata=EvaluationMetadata(
            dataset_version=IDENTITY.dataset_version,
            dataset_hash=IDENTITY.dataset_hash,
            index_identity=IDENTITY.index_identity,
            parser_version=IDENTITY.parser_version,
            chunk_version=IDENTITY.chunk_version,
            retrieval_pipeline="dense-bm25-rrf-v1",
            embedding_model="embedding-v1",
            rerank_model="rerank-v1",
            configuration_fingerprint=IDENTITY.configuration_fingerprint,
            comparison_key="pilot-v1",
            executed_at=NOW.isoformat(),
            elapsed_ms=1_000.0,
            index_build_hash=IDENTITY.index_build_hash,
            routes=(
                EvaluationRoute(
                    data_classification=DataClassification.ORDINARY,
                    embedding_model="embedding-v1",
                    embedding_deployment="embedding-deployment-v1",
                    embedding_dimension=1_024,
                    rerank_model="rerank-v1",
                    rerank_deployment="rerank-deployment-v1",
                    provider_boundary=ProviderBoundary.INTERNAL,
                    production_ready=True,
                    request_count=20,
                    input_tokens=0,
                    configuration_fingerprint="ordinary-route-v1",
                    retrieval_config={},
                ),
            ),
        ),
        failures=(),
        category_metrics={},
        gate=QualityGate(
            decision=GateDecision.PASS,
            production_baseline_approved=True,
            framework_complete=True,
            reasons=(),
        ),
        answer_quality=AnswerQualityReport(
            dataset_version=IDENTITY.dataset_version,
            dataset_hash=IDENTITY.dataset_hash,
            metrics=AnswerQualityMetrics(
                case_count=20,
                citation_support_rate=0.98,
                answer_refusal_accuracy=0.95,
            ),
            gate=AnswerQualityGate(decision=GateDecision.PASS, reasons=()),
            route_fingerprints=tuple(sorted(IDENTITY.answer_route_fingerprints)),
        ),
    )


@pytest.mark.asyncio
async def test_evaluate_passes_only_with_complete_current_safe_evidence() -> None:
    policy = PilotPolicy()
    inputs = ReadinessInputs(
        identity=IDENTITY,
        configuration=_bundle(policy.required_configuration_checks),
        dependencies=_bundle(policy.required_dependency_checks),
        evaluation=_evaluation(),
        runtime=RuntimeEvidence(
            observed_at=NOW,
            identity_fingerprint=IDENTITY.fingerprint,
            signals=policy.required_runtime_signals,
            exporter_failure_count=1,
            exporter_alert_count=1,
            unsafe_attribute_count=0,
            trace_continuity_count=1,
            trace_chains=policy.required_trace_chains,
        ),
        security=SecurityEvidence(
            observed_at=NOW,
            identity_fingerprint=IDENTITY.fingerprint,
            invalid_citation_count=0,
            missing_critical_audit_event_count=0,
            audit_trace_link_count=20,
        ),
        infrastructure=_bundle(policy.required_infrastructure_checks),
        capacity=CapacityEvidence(
            observed_at=NOW,
            identity_fingerprint=IDENTITY.fingerprint,
            reference="load://pilot-v1",
            throughput_rps=12.0,
            latency_p50_ms=180.0,
            latency_p95_ms=650.0,
            error_rate=0.005,
            cpu_high_watermark=0.70,
            memory_high_watermark=0.72,
        ),
        recovery=_bundle(policy.required_recovery_checks),
    )
    readiness = PilotReadiness(
        inputs,
        policy=policy,
        clock=lambda: NOW,
        id_factory=lambda: UUID("00000000-0000-0000-0000-000000000009"),
    )

    report = await readiness.evaluate()

    assert report.decision is GateDecision.PASS
    assert {gate.name for gate in report.gates} == {
        "configuration",
        "dependencies",
        "retrieval_quality",
        "answer_quality",
        "security",
        "telemetry",
        "infrastructure",
        "capacity",
        "recovery",
    }
    payload = report.to_dict()
    assert payload["reportVersion"] == "pilot-readiness-v1"
    assert payload["reportId"] == "00000000-0000-0000-0000-000000000009"
    assert "question" not in str(payload).casefold()
    assert "prompt" not in str(payload).casefold()
    assert "credential" not in str(payload).casefold()

    without_authorization_denial = await PilotReadiness(
        replace(
            inputs,
            runtime=replace(
                inputs.runtime,
                signals=inputs.runtime.signals - {"authorization_denial"},
            ),
        ),
        policy=policy,
        clock=lambda: NOW,
    ).evaluate()
    telemetry_gate = next(
        gate
        for gate in without_authorization_denial.gates
        if gate.name == "telemetry"
    )
    assert telemetry_gate.decision is GateDecision.FAIL
    assert "SIGNAL_MISSING:authorization_denial" in telemetry_gate.reason_codes


@pytest.mark.asyncio
async def test_missing_evidence_is_indeterminate_instead_of_passing() -> None:
    report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY),
        clock=lambda: NOW,
    ).evaluate()

    assert report.decision is GateDecision.INDETERMINATE
    assert all(gate.decision is GateDecision.INDETERMINATE for gate in report.gates)


@pytest.mark.asyncio
async def test_hard_security_failure_precedes_indeterminate_gates() -> None:
    evaluation = _evaluation()
    evaluation = replace(
        evaluation,
        metrics=replace(evaluation.metrics, tenant_leakage_count=1),
    )
    report = await PilotReadiness(
        ReadinessInputs(
            identity=IDENTITY,
            evaluation=evaluation,
            security=SecurityEvidence(
                observed_at=NOW,
                identity_fingerprint=IDENTITY.fingerprint,
                invalid_citation_count=1,
                missing_critical_audit_event_count=1,
                audit_trace_link_count=0,
            ),
        ),
        clock=lambda: NOW,
    ).evaluate()

    assert report.decision is GateDecision.FAIL
    gates = {gate.name: gate for gate in report.gates}
    assert "TENANT_LEAKAGE_DETECTED" in gates["retrieval_quality"].reason_codes
    assert gates["security"].reason_codes == (
        "INVALID_CITATION_DETECTED",
        "CRITICAL_AUDIT_EVENT_MISSING",
        "AUDIT_TRACE_LINK_MISSING",
    )
    assert gates["configuration"].decision is GateDecision.INDETERMINATE

    leakage_without_quality_metrics = replace(
        evaluation,
        metrics=replace(evaluation.metrics, recall_at_5=None, mrr_at_5=None),
    )
    leakage_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=leakage_without_quality_metrics),
        clock=lambda: NOW,
    ).evaluate()
    leakage_gate = next(gate for gate in leakage_report.gates if gate.name == "retrieval_quality")
    assert leakage_gate.decision is GateDecision.FAIL
    assert leakage_gate.reason_codes == ("TENANT_LEAKAGE_DETECTED",)


@pytest.mark.asyncio
async def test_stale_or_mismatched_evaluation_is_rejected() -> None:
    stale = _evaluation()
    stale = replace(
        stale,
        metadata=replace(
            stale.metadata,
            executed_at=(NOW - timedelta(days=2)).isoformat(),
        ),
    )
    stale_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=stale),
        clock=lambda: NOW,
    ).evaluate()
    mismatch_report = await PilotReadiness(
        ReadinessInputs(
            identity=replace(IDENTITY, index_build_hash="different-index-build"),
            evaluation=_evaluation(),
        ),
        clock=lambda: NOW,
    ).evaluate()

    stale_gates = {gate.name: gate for gate in stale_report.gates}
    mismatch_gates = {gate.name: gate for gate in mismatch_report.gates}
    assert stale_gates["retrieval_quality"].reason_codes == ("EVALUATION_STALE",)
    assert mismatch_gates["answer_quality"].reason_codes == ("EVALUATION_IDENTITY_MISMATCH",)

    mismatched_answer = replace(
        _evaluation(),
        answer_quality=replace(
            _evaluation().answer_quality,
            dataset_hash="different-answer-dataset",
        ),
    )
    answer_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=mismatched_answer),
        clock=lambda: NOW,
    ).evaluate()
    answer_gate = next(gate for gate in answer_report.gates if gate.name == "answer_quality")
    assert answer_gate.reason_codes == ("ANSWER_EVALUATION_IDENTITY_MISMATCH",)

    non_production_route = replace(
        _evaluation(),
        metadata=replace(
            _evaluation().metadata,
            routes=(replace(_evaluation().metadata.routes[0], production_ready=False),),
        ),
    )
    route_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=non_production_route),
        clock=lambda: NOW,
    ).evaluate()
    route_gate = next(
        gate for gate in route_report.gates if gate.name == "retrieval_quality"
    )
    assert route_gate.reason_codes == ("EVALUATION_ROUTE_NOT_PRODUCTION_READY",)


class FailingTelemetryExporter:
    def export(self, event: object) -> None:
        del event
        raise RuntimeError("export unavailable")


@pytest.mark.asyncio
async def test_unsafe_telemetry_is_dropped_and_blocks_readiness_without_breaking_work() -> None:
    recorder = RecordingTelemetryExporter()
    telemetry = OperationalTelemetry(
        (recorder, FailingTelemetryExporter()),
        release_fingerprint=IDENTITY.fingerprint,
        clock=lambda: NOW,
    )
    trace_id = UUID("00000000-0000-0000-0000-000000000099")
    for signal in PilotPolicy().required_runtime_signals:
        telemetry.emit(
            trace_id=trace_id,
            signal=signal,
            stage=signal,
            outcome="SUCCEEDED",
            attributes={
                "duration_ms": 12.0,
                "decision": "PASS",
                "question": "sensitive employee question",
                "token": "secret-token",
                "reason_code": "sensitive employee question",
            },
        )

    snapshot = telemetry.snapshot()
    report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, runtime=snapshot),
        clock=lambda: NOW,
    ).evaluate()

    assert all(
        dict(event.attributes) == {"decision": "PASS", "duration_ms": 12.0}
        for event in recorder.events
    )
    assert all("sensitive" not in repr(event) for event in recorder.events)
    telemetry_gate = next(gate for gate in report.gates if gate.name == "telemetry")
    assert telemetry_gate.decision is GateDecision.FAIL
    assert telemetry_gate.reason_codes == ("UNSAFE_TELEMETRY_ATTRIBUTE_DETECTED",)
    assert snapshot.exporter_failure_count == snapshot.exporter_alert_count
    assert snapshot.trace_continuity_count == 1


@pytest.mark.asyncio
async def test_configuration_and_queue_probes_feed_the_unified_readiness_seam(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    configuration = production_configuration_evidence(
        Settings(
            identity_provider="oidc",
            oidc_issuer="https://identity.example.test",
            oidc_audience="erp-assistant",
            oidc_jwks_url="https://identity.example.test/keys",
            oidc_authorization_url="https://identity.example.test/authorize",
            oidc_token_url="https://identity.example.test/token",
            oidc_client_id="erp-assistant-web",
        ),
        ProductionConfigurationProbe(
            model_routes_available=False,
            index_dimensions_match=True,
            database_migration_current=True,
            worker_available=True,
        ),
        observed_at=NOW,
        identity_fingerprint=IDENTITY.fingerprint,
        reference="probe://production-configuration/run-9",
    )
    telemetry = OperationalTelemetry(
        release_fingerprint=IDENTITY.fingerprint,
        clock=lambda: NOW,
    )
    trace_id = UUID("00000000-0000-0000-0000-000000000109")
    async with session_factory() as session:
        session.add_all(
            [
                KnowledgeIndexJob(
                    document_id=UUID("00000000-0000-0000-0000-000000000301"),
                    status="QUEUED",
                    phase="QUEUED",
                    attempt=3,
                ),
                KnowledgeIndexJob(
                    document_id=UUID("00000000-0000-0000-0000-000000000302"),
                    status="FAILED",
                    phase="INDEXING",
                    attempt=1,
                ),
                KnowledgeIndexJob(
                    document_id=UUID("00000000-0000-0000-0000-000000000303"),
                    status="RUNNING",
                    phase="INDEXING",
                    attempt=2,
                    lease_expires_at=NOW - timedelta(minutes=1),
                ),
                KnowledgeIndexJob(
                    document_id=UUID("00000000-0000-0000-0000-000000000304"),
                    status="FAILED",
                    phase="INDEXING",
                    attempt=5,
                    created_at=NOW - timedelta(minutes=2),
                ),
                KnowledgeIndexJob(
                    document_id=UUID("00000000-0000-0000-0000-000000000304"),
                    status="SUCCEEDED",
                    phase="READY",
                    attempt=2,
                    created_at=NOW - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()
    queue_snapshot = await SqlIndexQueueMonitor(
        session_factory,
        telemetry,
        clock=lambda: NOW,
    ).observe(
        trace_id=trace_id,
    )

    report = await PilotReadiness(
        ReadinessInputs(
            identity=IDENTITY,
            configuration=configuration,
            runtime=telemetry.snapshot(),
        ),
        clock=lambda: NOW,
    ).evaluate()

    gates = {gate.name: gate for gate in report.gates}
    assert gates["configuration"].decision is GateDecision.FAIL
    assert gates["configuration"].reason_codes == ("CONFIG_MODEL_ROUTES_INVALID",)
    queue_evidence = telemetry.snapshot()
    assert {"queue_depth", "retry_count", "failed_job", "stuck_job"} <= (queue_evidence.signals)
    assert {"failed_job", "stuck_job"} <= queue_evidence.failed_signals
    assert "SIGNAL_FAILED:stuck_job" in gates["telemetry"].reason_codes
    assert queue_snapshot.queue_depth == 1
    assert queue_snapshot.failed_job_count == 1
    assert queue_snapshot.retry_count == 4


@pytest.mark.asyncio
async def test_unavailable_production_probes_return_evidence_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable_async() -> bool:
        raise ConnectionError("database unavailable")

    def unavailable_worker() -> bool:
        raise TimeoutError("worker unavailable")

    def unavailable_index(settings: Settings) -> bool:
        del settings
        raise ConnectionError("milvus unavailable")

    monkeypatch.setattr(production_probes, "_migration_is_current", unavailable_async)
    monkeypatch.setattr(production_probes, "_worker_is_available", unavailable_worker)
    monkeypatch.setattr(production_probes, "_index_dimensions_match", unavailable_index)

    evidence = await production_probes.collect_production_configuration(
        Settings(),
        IDENTITY,
        observed_at=NOW,
    )

    decisions = {check.name: check.decision for check in evidence.checks}
    assert decisions["database_migrations"] is GateDecision.INDETERMINATE
    assert decisions["worker"] is GateDecision.INDETERMINATE
    assert decisions["index_dimensions"] is GateDecision.INDETERMINATE


def test_production_identity_allows_pre_registered_rollback_binding() -> None:
    active = SimpleNamespace(data_classification="ORDINARY")

    class Registry:
        bindings = (
            SimpleNamespace(data_classification=DataClassification.ORDINARY),
            SimpleNamespace(data_classification=DataClassification.ORDINARY),
        )

        @staticmethod
        def can_resolve(profile: object) -> bool:
            return profile is active

    selected = production_probes._matching_active_profiles(  # type: ignore[arg-type]
        Registry(),
        (active,),  # type: ignore[arg-type]
    )

    assert selected == (active,)


@pytest.mark.asyncio
async def test_failed_check_precedes_missing_check_and_unsafe_reference_is_rejected() -> None:
    evidence = EvidenceBundle(
        observed_at=NOW,
        identity_fingerprint=IDENTITY.fingerprint,
        checks=(
            EvidenceCheck(
                name="model_routes",
                decision=GateDecision.FAIL,
                reference="probe://model-routes/run-9",
                reason_code="MODEL_ROUTE_UNAVAILABLE",
            ),
        ),
    )
    failed = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, dependencies=evidence),
        clock=lambda: NOW,
    ).evaluate()
    unsafe = await PilotReadiness(
        ReadinessInputs(
            identity=IDENTITY,
            dependencies=replace(
                evidence,
                checks=(
                    replace(
                        evidence.checks[0],
                        decision=GateDecision.PASS,
                        reference="https://probe.test/run?token=secret",
                    ),
                ),
            ),
        ),
        clock=lambda: NOW,
    ).evaluate()

    failed_gate = next(gate for gate in failed.gates if gate.name == "dependencies")
    unsafe_gate = next(gate for gate in unsafe.gates if gate.name == "dependencies")
    assert failed_gate.decision is GateDecision.FAIL
    assert failed_gate.reason_codes == ("MODEL_ROUTE_UNAVAILABLE",)
    assert unsafe_gate.reason_codes == ("EVIDENCE_REFERENCE_INVALID",)
    assert unsafe_gate.evidence_references == ()


@pytest.mark.asyncio
async def test_invalid_metrics_and_unapproved_baseline_fail_closed() -> None:
    invalid = replace(
        _evaluation(),
        metrics=replace(_evaluation().metrics, recall_at_5=float("nan")),
    )
    unapproved = replace(
        _evaluation(),
        gate=replace(_evaluation().gate, production_baseline_approved=False),
    )

    invalid_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=invalid), clock=lambda: NOW
    ).evaluate()
    unapproved_report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, evaluation=unapproved), clock=lambda: NOW
    ).evaluate()

    invalid_gate = next(gate for gate in invalid_report.gates if gate.name == "retrieval_quality")
    unapproved_gate = next(
        gate for gate in unapproved_report.gates if gate.name == "retrieval_quality"
    )
    assert invalid_gate.reason_codes == ("RETRIEVAL_METRIC_INVALID",)
    assert unapproved_gate.reason_codes == ("PRODUCTION_BASELINE_NOT_APPROVED",)


@pytest.mark.asyncio
async def test_runtime_evidence_excludes_stale_and_cross_release_events() -> None:
    recorder = RecordingTelemetryExporter()
    stale = OperationalTelemetry(
        (recorder,),
        release_fingerprint=IDENTITY.fingerprint,
        clock=lambda: NOW - timedelta(days=2),
    )
    stale.emit(
        trace_id=UUID("00000000-0000-0000-0000-000000000209"),
        signal="http_request",
        stage="http",
        outcome="SUCCEEDED",
    )
    other_release = OperationalTelemetry(
        (recorder,),
        release_fingerprint="other-release-fingerprint",
        clock=lambda: NOW,
    )
    other_release.emit(
        trace_id=UUID("00000000-0000-0000-0000-000000000210"),
        signal="http_request",
        stage="http",
        outcome="SUCCEEDED",
    )
    evidence = runtime_evidence_from_events(
        recorder.events,
        identity_fingerprint=IDENTITY.fingerprint,
        window_end=NOW,
        window=timedelta(hours=24),
    )

    report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, runtime=evidence),
        clock=lambda: NOW,
    ).evaluate()

    gate = next(gate for gate in report.gates if gate.name == "telemetry")
    assert evidence.signals == frozenset()
    assert gate.reason_codes == ("EVIDENCE_STALE",)


def test_release_identity_rejects_unsafe_persisted_values() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        replace(IDENTITY, release_version="https://release.test?token=secret")


def test_release_adapter_preserves_evidence_identity_and_requires_json_booleans() -> None:
    evidence = release_check._bundle(
        {
            "observedAt": NOW.isoformat(),
            "identityFingerprint": "previous-release-fingerprint",
            "checks": [],
        }
    )
    route = _evaluation().metadata.routes[0]
    payload = {
        "dataClassification": route.data_classification.value,
        "embeddingModel": route.embedding_model,
        "embeddingDeployment": route.embedding_deployment,
        "embeddingDimension": route.embedding_dimension,
        "rerankModel": route.rerank_model,
        "rerankDeployment": route.rerank_deployment,
        "providerBoundary": route.provider_boundary.value,
        "productionReady": "false",
        "requestCount": route.request_count,
        "inputTokens": route.input_tokens,
        "configurationFingerprint": route.configuration_fingerprint,
        "retrievalConfig": route.retrieval_config,
    }

    assert evidence.identity_fingerprint == "previous-release-fingerprint"
    with pytest.raises(ValueError, match="JSON booleans"):
        release_check._route(payload)


def test_production_startup_fails_closed_before_serving_requests() -> None:
    assert validate_production_startup(Settings(), observed_at=NOW) is None
    with pytest.raises(RuntimeError, match="Production startup configuration invalid"):
        validate_production_startup(
            Settings(app_env="production"),
            observed_at=NOW,
        )


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, list[str]] = {}

    def pipeline(self, *, transaction: bool) -> "FakeRedis":
        assert transaction is False
        return self

    def lpush(self, key: str, value: str) -> "FakeRedis":
        self.values.setdefault(key, []).insert(0, value)
        return self

    def ltrim(self, key: str, start: int, end: int) -> "FakeRedis":
        self.values[key] = self.values.get(key, [])[start : end + 1]
        return self

    def expire(self, key: str, seconds: int) -> "FakeRedis":
        assert key in self.values and seconds > 0
        return self

    def execute(self) -> list[object]:
        return []

    def hgetall(self, key: str) -> dict[str, str]:
        del key
        return {}

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        values = self.values.get(key, [])
        return values[start:] if end == -1 else values[start : end + 1]


class BlockingRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def execute(self) -> list[object]:
        self.started.set()
        self.release.wait(1.0)
        return []


def test_shared_redis_flush_has_a_total_deadline_and_explicit_close() -> None:
    store = RedisTelemetryStore("redis://unused")
    redis = BlockingRedis()
    store._redis = redis  # type: ignore[assignment]
    telemetry = OperationalTelemetry(
        (store,), release_fingerprint=IDENTITY.fingerprint, clock=lambda: NOW
    )
    telemetry.emit(
        trace_id=UUID("00000000-0000-0000-0000-000000000308"),
        signal="http_request",
        stage="http",
        outcome="SUCCEEDED",
    )
    assert redis.started.wait(1.0)

    assert store.flush(timeout=0.01) is False
    assert store._pending_failures == 1
    redis.release.set()
    assert store.close(timeout=1.0) is True


@pytest.mark.asyncio
async def test_shared_redis_events_form_cross_process_trace_evidence() -> None:
    store = RedisTelemetryStore("redis://unused")
    store._redis = FakeRedis()  # type: ignore[assignment]
    trace_id = UUID("00000000-0000-0000-0000-000000000309")
    web = OperationalTelemetry(
        (store,), release_fingerprint=IDENTITY.fingerprint, clock=lambda: NOW
    )
    worker = OperationalTelemetry(
        (store,), release_fingerprint=IDENTITY.fingerprint, clock=lambda: NOW
    )
    for index, signal in enumerate(sorted(PilotPolicy().required_runtime_signals)):
        (web if index % 2 == 0 else worker).emit(
            trace_id=trace_id,
            signal=signal,
            stage=signal,
            outcome="SUCCEEDED",
        )

    evidence = store.evidence(
        identity_fingerprint=IDENTITY.fingerprint,
        window_end=NOW,
    )
    report = await PilotReadiness(
        ReadinessInputs(identity=IDENTITY, runtime=evidence), clock=lambda: NOW
    ).evaluate()

    gate = next(gate for gate in report.gates if gate.name == "telemetry")
    assert evidence.trace_chains == PilotPolicy().required_trace_chains
    assert gate.decision is GateDecision.PASS


@pytest.mark.asyncio
async def test_evaluate_appends_an_immutable_versioned_report(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report_id = UUID("00000000-0000-0000-0000-000000000019")
    readiness = PilotReadiness(
        ReadinessInputs(identity=IDENTITY),
        store=SqlReadinessReportStore(session_factory),
        clock=lambda: NOW,
        id_factory=lambda: report_id,
    )

    report = await readiness.evaluate()

    assert report.report_id == report_id
    with pytest.raises(ValueError, match="immutable"):
        await readiness.evaluate()
