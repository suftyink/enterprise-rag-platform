import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID

from erp_assistant.evaluation.models import EvaluationReport, GateDecision

SAFE_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+|\-]{0,255}$")


@dataclass(frozen=True)
class ReleaseIdentity:
    release_version: str
    dataset_version: str
    dataset_hash: str
    index_identity: str
    index_build_hash: str
    parser_version: str
    chunk_version: str
    configuration_fingerprint: str
    route_fingerprints: frozenset[str]
    answer_route_fingerprints: frozenset[str]

    def __post_init__(self) -> None:
        values = (
            self.release_version,
            self.dataset_version,
            self.dataset_hash,
            self.index_identity,
            self.index_build_hash,
            self.parser_version,
            self.chunk_version,
            self.configuration_fingerprint,
            *self.route_fingerprints,
            *self.answer_route_fingerprints,
        )
        if not self.route_fingerprints or not self.answer_route_fingerprints:
            raise ValueError("Release identity requires retrieval and answer routes")
        if any(SAFE_IDENTITY_VALUE.fullmatch(value) is None for value in values):
            raise ValueError("Release identity contains an unsafe value")

    @property
    def fingerprint(self) -> str:
        payload = {
            "chunkVersion": self.chunk_version,
            "configurationFingerprint": self.configuration_fingerprint,
            "datasetHash": self.dataset_hash,
            "datasetVersion": self.dataset_version,
            "indexBuildHash": self.index_build_hash,
            "indexIdentity": self.index_identity,
            "parserVersion": self.parser_version,
            "releaseVersion": self.release_version,
            "routeFingerprints": sorted(self.route_fingerprints),
            "answerRouteFingerprints": sorted(self.answer_route_fingerprints),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class EvidenceCheck:
    name: str
    decision: GateDecision
    reference: str
    reason_code: str | None = None


@dataclass(frozen=True)
class EvidenceBundle:
    observed_at: datetime
    identity_fingerprint: str
    checks: tuple[EvidenceCheck, ...]


@dataclass(frozen=True)
class RuntimeEvidence:
    observed_at: datetime
    identity_fingerprint: str
    signals: frozenset[str]
    exporter_failure_count: int
    exporter_alert_count: int
    unsafe_attribute_count: int
    trace_continuity_count: int
    failed_signals: frozenset[str] = field(default_factory=frozenset)
    trace_chains: frozenset[str] = field(default_factory=frozenset)
    reference: str = "runtime://memory"


@dataclass(frozen=True)
class SecurityEvidence:
    observed_at: datetime
    identity_fingerprint: str
    invalid_citation_count: int
    missing_critical_audit_event_count: int
    audit_trace_link_count: int
    reference: str = "security://audit-summary"


@dataclass(frozen=True)
class CapacityEvidence:
    observed_at: datetime
    identity_fingerprint: str
    reference: str
    throughput_rps: float
    latency_p50_ms: float
    latency_p95_ms: float
    error_rate: float
    cpu_high_watermark: float
    memory_high_watermark: float


@dataclass(frozen=True)
class PilotPolicy:
    maximum_evidence_age: timedelta = timedelta(hours=24)
    minimum_recall_at_5: float = 0.90
    minimum_mrr_at_5: float = 0.80
    minimum_refusal_accuracy: float = 0.90
    minimum_citation_support_rate: float = 0.95
    minimum_throughput_rps: float = 10.0
    maximum_latency_p95_ms: float = 1_500.0
    maximum_error_rate: float = 0.01
    maximum_resource_high_watermark: float = 0.85
    required_configuration_checks: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "oidc",
                "model_routes",
                "index_dimensions",
                "database_migrations",
                "storage",
                "worker",
                "release_identity",
            }
        )
    )
    required_dependency_checks: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"postgres", "redis", "minio", "milvus", "celery", "model_routes"}
        )
    )
    required_runtime_signals: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "authorization_denial",
                "http_request",
                "parsing",
                "indexing",
                "retrieval",
                "embedding",
                "recall",
                "filtering",
                "rerank",
                "retrieval_gate",
                "answer",
                "model_generation",
                "queue_depth",
                "retry_count",
                "failed_job",
                "stuck_job",
                "lifecycle_transition",
            }
        )
    )
    required_trace_chains: frozenset[str] = field(
        default_factory=lambda: frozenset({"indexing", "retrieval", "answer"})
    )
    required_infrastructure_checks: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "upload",
                "index",
                "retrieve",
                "answer",
                "celery_retry",
                "worker_interruption",
                "backlog_alert",
            }
        )
    )
    required_recovery_checks: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "database_restore",
                "index_rebuild",
                "profile_rollback",
                "cleanup_retry",
                "published_version_continuity",
            }
        )
    )


@dataclass(frozen=True)
class ReadinessInputs:
    identity: ReleaseIdentity
    configuration: EvidenceBundle | None = None
    dependencies: EvidenceBundle | None = None
    evaluation: EvaluationReport | None = None
    runtime: RuntimeEvidence | None = None
    security: SecurityEvidence | None = None
    infrastructure: EvidenceBundle | None = None
    capacity: CapacityEvidence | None = None
    recovery: EvidenceBundle | None = None


@dataclass(frozen=True)
class ReadinessGate:
    name: str
    decision: GateDecision
    reason_codes: tuple[str, ...]
    evidence_references: tuple[str, ...] = ()
    summary: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class PilotReadinessReport:
    report_id: UUID
    identity: ReleaseIdentity
    generated_at: datetime
    decision: GateDecision
    gates: tuple[ReadinessGate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "reportVersion": "pilot-readiness-v1",
            "reportId": str(self.report_id),
            "generatedAt": self.generated_at.isoformat(),
            "decision": self.decision.value,
            "release": {
                "releaseVersion": self.identity.release_version,
                "fingerprint": self.identity.fingerprint,
                "datasetVersion": self.identity.dataset_version,
                "datasetHash": self.identity.dataset_hash,
                "indexIdentity": self.identity.index_identity,
                "indexBuildHash": self.identity.index_build_hash,
                "parserVersion": self.identity.parser_version,
                "chunkVersion": self.identity.chunk_version,
                "configurationFingerprint": self.identity.configuration_fingerprint,
                "routeFingerprints": sorted(self.identity.route_fingerprints),
                "answerRouteFingerprints": sorted(self.identity.answer_route_fingerprints),
            },
            "gates": [
                {
                    "name": gate.name,
                    "decision": gate.decision.value,
                    "reasonCodes": list(gate.reason_codes),
                    "evidenceReferences": list(gate.evidence_references),
                    "summary": dict(gate.summary),
                }
                for gate in self.gates
            ],
        }
