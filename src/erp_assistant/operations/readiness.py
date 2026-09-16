import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from math import isfinite
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from erp_assistant.evaluation.models import EvaluationReport, GateDecision
from erp_assistant.operations.readiness_models import (
    EvidenceBundle,
    PilotPolicy,
    PilotReadinessReport,
    ReadinessGate,
    ReadinessInputs,
)
from erp_assistant.operations.readiness_store import ReadinessReportStore

SAFE_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_:\-]{0,127}$")
SAFE_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}$")


class PilotReadiness:
    def __init__(
        self,
        inputs: ReadinessInputs,
        *,
        policy: PilotPolicy | None = None,
        store: ReadinessReportStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._inputs = inputs
        self._policy = policy or PilotPolicy()
        self._store = store
        self._clock = clock
        self._id_factory = id_factory

    async def evaluate(self) -> PilotReadinessReport:
        now = self._clock()
        gates = (
            self._bundle_gate(
                "configuration",
                self._inputs.configuration,
                self._policy.required_configuration_checks,
                now,
            ),
            self._bundle_gate(
                "dependencies",
                self._inputs.dependencies,
                self._policy.required_dependency_checks,
                now,
            ),
            self._retrieval_gate(now),
            self._answer_gate(now),
            self._security_gate(now),
            self._runtime_gate(now),
            self._bundle_gate(
                "infrastructure",
                self._inputs.infrastructure,
                self._policy.required_infrastructure_checks,
                now,
            ),
            self._capacity_gate(now),
            self._bundle_gate(
                "recovery",
                self._inputs.recovery,
                self._policy.required_recovery_checks,
                now,
            ),
        )
        decision = self._aggregate(gate.decision for gate in gates)
        report = PilotReadinessReport(
            report_id=self._id_factory(),
            identity=self._inputs.identity,
            generated_at=now,
            decision=decision,
            gates=gates,
        )
        if self._store is not None:
            await self._store.append(report)
        return report

    def _bundle_gate(
        self,
        name: str,
        evidence: EvidenceBundle | None,
        required: frozenset[str],
        now: datetime,
    ) -> ReadinessGate:
        if evidence is None:
            return self._gate(name, GateDecision.INDETERMINATE, "EVIDENCE_MISSING")
        invalid = self._common_evidence_reason(
            evidence.observed_at, evidence.identity_fingerprint, now
        )
        if invalid is not None:
            return self._gate(name, GateDecision.FAIL, invalid)
        checks = {check.name: check for check in evidence.checks}
        if len(checks) != len(evidence.checks):
            return self._gate(name, GateDecision.FAIL, "DUPLICATE_EVIDENCE_CHECK")
        missing = sorted(required - checks.keys())
        selected = tuple(checks[item] for item in sorted(required & checks.keys()))
        if any(not self._safe_reference(check.reference) for check in selected):
            return self._gate(name, GateDecision.FAIL, "EVIDENCE_REFERENCE_INVALID")
        if any(
            check.reason_code is not None and SAFE_REASON_CODE.fullmatch(check.reason_code) is None
            for check in selected
        ):
            return self._gate(name, GateDecision.FAIL, "EVIDENCE_REASON_CODE_INVALID")
        failed = tuple(check for check in selected if check.decision is GateDecision.FAIL)
        if failed:
            return ReadinessGate(
                name,
                GateDecision.FAIL,
                tuple(check.reason_code or f"CHECK_FAIL:{check.name}" for check in failed),
                tuple(check.reference for check in selected),
            )
        if missing:
            return ReadinessGate(
                name,
                GateDecision.INDETERMINATE,
                tuple(f"CHECK_MISSING:{item}" for item in missing),
                tuple(check.reference for check in selected),
            )
        decision = self._aggregate(check.decision for check in selected)
        reasons = tuple(
            check.reason_code or f"CHECK_{check.decision.value}:{check.name}"
            for check in selected
            if check.decision is not GateDecision.PASS
        )
        return ReadinessGate(
            name,
            decision,
            reasons,
            tuple(check.reference for check in selected),
        )

    def _retrieval_gate(self, now: datetime) -> ReadinessGate:
        report = self._inputs.evaluation
        invalid = self._evaluation_reason(report, now)
        if invalid is not None:
            decision = (
                GateDecision.INDETERMINATE if invalid == "EVALUATION_MISSING" else GateDecision.FAIL
            )
            return self._gate("retrieval_quality", decision, invalid)
        assert report is not None
        metrics = report.metrics
        if (
            metrics.case_count < 0
            or metrics.evaluated_case_count < 0
            or metrics.evaluated_case_count > metrics.case_count
            or metrics.tenant_leakage_count < 0
            or not all(
                isfinite(value) and value >= 0
                for value in (metrics.latency_p50_ms, metrics.latency_p95_ms)
            )
            or metrics.latency_p95_ms < metrics.latency_p50_ms
            or (metrics.ndcg_at_5 is not None and not self._valid_ratio(metrics.ndcg_at_5))
        ):
            return self._gate("retrieval_quality", GateDecision.FAIL, "RETRIEVAL_METRIC_INVALID")
        approval_reasons: list[str] = []
        if report.gate.decision is not GateDecision.PASS:
            approval_reasons.append("RETRIEVAL_EVALUATION_NOT_APPROVED")
        if not report.gate.production_baseline_approved:
            approval_reasons.append("PRODUCTION_BASELINE_NOT_APPROVED")
        if not report.gate.framework_complete:
            approval_reasons.append("EVALUATION_FRAMEWORK_INCOMPLETE")
        if metrics.tenant_leakage_count > 0:
            approval_reasons.append("TENANT_LEAKAGE_DETECTED")
        if approval_reasons:
            return ReadinessGate("retrieval_quality", GateDecision.FAIL, tuple(approval_reasons))
        if metrics.recall_at_5 is None or metrics.mrr_at_5 is None:
            return self._gate(
                "retrieval_quality", GateDecision.INDETERMINATE, "RETRIEVAL_METRICS_MISSING"
            )
        if not all(self._valid_ratio(value) for value in (metrics.recall_at_5, metrics.mrr_at_5)):
            return self._gate("retrieval_quality", GateDecision.FAIL, "RETRIEVAL_METRIC_INVALID")
        reasons: list[str] = []
        if metrics.recall_at_5 < self._policy.minimum_recall_at_5:
            reasons.append("RECALL_AT_5_BELOW_TARGET")
        if metrics.mrr_at_5 < self._policy.minimum_mrr_at_5:
            reasons.append("MRR_AT_5_BELOW_TARGET")
        if metrics.refusal_accuracy is None:
            return self._gate(
                "retrieval_quality", GateDecision.INDETERMINATE, "REFUSAL_METRIC_MISSING"
            )
        if not self._valid_ratio(metrics.refusal_accuracy):
            return self._gate("retrieval_quality", GateDecision.FAIL, "REFUSAL_METRIC_INVALID")
        if metrics.refusal_accuracy < self._policy.minimum_refusal_accuracy:
            reasons.append("REFUSAL_ACCURACY_BELOW_TARGET")
        return ReadinessGate(
            "retrieval_quality",
            GateDecision.FAIL if reasons else GateDecision.PASS,
            tuple(reasons),
            (report.metadata.comparison_key,),
        )

    def _answer_gate(self, now: datetime) -> ReadinessGate:
        report = self._inputs.evaluation
        invalid = self._evaluation_reason(report, now)
        if invalid is not None:
            decision = (
                GateDecision.INDETERMINATE if invalid == "EVALUATION_MISSING" else GateDecision.FAIL
            )
            return self._gate("answer_quality", decision, invalid)
        assert report is not None
        answer = report.answer_quality
        if answer is None:
            return self._gate(
                "answer_quality", GateDecision.INDETERMINATE, "ANSWER_EVALUATION_MISSING"
            )
        if (
            answer.dataset_version != self._inputs.identity.dataset_version
            or answer.dataset_hash != self._inputs.identity.dataset_hash
            or frozenset(answer.route_fingerprints)
            != self._inputs.identity.answer_route_fingerprints
        ):
            return self._gate(
                "answer_quality", GateDecision.FAIL, "ANSWER_EVALUATION_IDENTITY_MISMATCH"
            )
        metrics = answer.metrics
        if answer.gate.decision is not GateDecision.PASS:
            return self._gate("answer_quality", GateDecision.FAIL, "ANSWER_EVALUATION_NOT_APPROVED")
        if metrics.citation_support_rate is None or metrics.answer_refusal_accuracy is None:
            return self._gate(
                "answer_quality", GateDecision.INDETERMINATE, "ANSWER_METRICS_MISSING"
            )
        if not all(
            self._valid_ratio(value)
            for value in (
                metrics.citation_support_rate,
                metrics.answer_refusal_accuracy,
            )
        ):
            return self._gate("answer_quality", GateDecision.FAIL, "ANSWER_METRIC_INVALID")
        reasons: list[str] = []
        if metrics.citation_support_rate < self._policy.minimum_citation_support_rate:
            reasons.append("CITATION_SUPPORT_BELOW_TARGET")
        if metrics.answer_refusal_accuracy < self._policy.minimum_refusal_accuracy:
            reasons.append("ANSWER_REFUSAL_ACCURACY_BELOW_TARGET")
        return ReadinessGate(
            "answer_quality",
            GateDecision.FAIL if reasons else GateDecision.PASS,
            tuple(reasons),
            (report.metadata.comparison_key,),
        )

    def _security_gate(self, now: datetime) -> ReadinessGate:
        evidence = self._inputs.security
        if evidence is None:
            return self._gate("security", GateDecision.INDETERMINATE, "EVIDENCE_MISSING")
        invalid = self._common_evidence_reason(
            evidence.observed_at, evidence.identity_fingerprint, now
        )
        if invalid is not None:
            return self._gate("security", GateDecision.FAIL, invalid)
        if not self._safe_reference(evidence.reference):
            return self._gate("security", GateDecision.FAIL, "EVIDENCE_REFERENCE_INVALID")
        reasons: list[str] = []
        if (
            min(
                evidence.invalid_citation_count,
                evidence.missing_critical_audit_event_count,
                evidence.audit_trace_link_count,
            )
            < 0
        ):
            reasons.append("SECURITY_EVIDENCE_INVALID")
        if evidence.invalid_citation_count:
            reasons.append("INVALID_CITATION_DETECTED")
        if evidence.missing_critical_audit_event_count:
            reasons.append("CRITICAL_AUDIT_EVENT_MISSING")
        if evidence.audit_trace_link_count < 1:
            reasons.append("AUDIT_TRACE_LINK_MISSING")
        return ReadinessGate(
            "security",
            GateDecision.FAIL if reasons else GateDecision.PASS,
            tuple(reasons),
            (evidence.reference,),
            (
                ("invalidCitationCount", evidence.invalid_citation_count),
                (
                    "missingCriticalAuditEventCount",
                    evidence.missing_critical_audit_event_count,
                ),
                ("auditTraceLinkCount", evidence.audit_trace_link_count),
            ),
        )

    def _runtime_gate(self, now: datetime) -> ReadinessGate:
        evidence = self._inputs.runtime
        if evidence is None:
            return self._gate("telemetry", GateDecision.INDETERMINATE, "EVIDENCE_MISSING")
        invalid = self._common_evidence_reason(
            evidence.observed_at, evidence.identity_fingerprint, now
        )
        if invalid is not None:
            return self._gate("telemetry", GateDecision.FAIL, invalid)
        if not self._safe_reference(evidence.reference):
            return self._gate("telemetry", GateDecision.FAIL, "EVIDENCE_REFERENCE_INVALID")
        reasons: list[str] = []
        if any(
            SAFE_OPAQUE_REFERENCE.fullmatch(item) is None
            for item in evidence.signals | evidence.failed_signals | evidence.trace_chains
        ):
            reasons.append("RUNTIME_EVIDENCE_INVALID")
        if (
            min(
                evidence.exporter_failure_count,
                evidence.exporter_alert_count,
                evidence.unsafe_attribute_count,
                evidence.trace_continuity_count,
            )
            < 0
        ):
            reasons.append("RUNTIME_EVIDENCE_INVALID")
        missing = sorted(self._policy.required_runtime_signals - evidence.signals)
        reasons.extend(f"SIGNAL_MISSING:{item}" for item in missing)
        reasons.extend(f"SIGNAL_FAILED:{item}" for item in sorted(evidence.failed_signals))
        missing_chains = sorted(self._policy.required_trace_chains - evidence.trace_chains)
        reasons.extend(f"TRACE_CHAIN_MISSING:{item}" for item in missing_chains)
        if evidence.unsafe_attribute_count:
            reasons.append("UNSAFE_TELEMETRY_ATTRIBUTE_DETECTED")
        if evidence.exporter_alert_count < evidence.exporter_failure_count:
            reasons.append("EXPORTER_FAILURE_NOT_ALERTED")
        if evidence.trace_continuity_count < 1:
            reasons.append("TRACE_CONTINUITY_MISSING")
        return ReadinessGate(
            "telemetry",
            GateDecision.FAIL if reasons else GateDecision.PASS,
            tuple(reasons),
            (evidence.reference,),
            (
                ("signalCount", len(evidence.signals)),
                ("failedSignalCount", len(evidence.failed_signals)),
                ("traceChainCount", len(evidence.trace_chains)),
                ("traceContinuityCount", evidence.trace_continuity_count),
                ("unsafeAttributeCount", evidence.unsafe_attribute_count),
                ("exporterFailureCount", evidence.exporter_failure_count),
                ("exporterAlertCount", evidence.exporter_alert_count),
            ),
        )

    def _capacity_gate(self, now: datetime) -> ReadinessGate:
        evidence = self._inputs.capacity
        if evidence is None:
            return self._gate("capacity", GateDecision.INDETERMINATE, "EVIDENCE_MISSING")
        invalid = self._common_evidence_reason(
            evidence.observed_at, evidence.identity_fingerprint, now
        )
        if invalid is not None:
            return self._gate("capacity", GateDecision.FAIL, invalid)
        reasons: list[str] = []
        capacity_values = (
            evidence.throughput_rps,
            evidence.latency_p50_ms,
            evidence.latency_p95_ms,
            evidence.error_rate,
            evidence.cpu_high_watermark,
            evidence.memory_high_watermark,
        )
        if (
            not all(isfinite(value) for value in capacity_values)
            or min(capacity_values[:3]) < 0
            or evidence.latency_p95_ms < evidence.latency_p50_ms
            or not self._valid_ratio(evidence.error_rate)
            or not self._valid_ratio(evidence.cpu_high_watermark)
            or not self._valid_ratio(evidence.memory_high_watermark)
        ):
            return self._gate("capacity", GateDecision.FAIL, "CAPACITY_EVIDENCE_INVALID")
        if not self._safe_reference(evidence.reference):
            return self._gate("capacity", GateDecision.FAIL, "EVIDENCE_REFERENCE_INVALID")
        if evidence.throughput_rps < self._policy.minimum_throughput_rps:
            reasons.append("THROUGHPUT_BELOW_TARGET")
        if evidence.latency_p95_ms > self._policy.maximum_latency_p95_ms:
            reasons.append("LATENCY_P95_ABOVE_TARGET")
        if evidence.error_rate > self._policy.maximum_error_rate:
            reasons.append("ERROR_RATE_ABOVE_TARGET")
        if max(evidence.cpu_high_watermark, evidence.memory_high_watermark) > (
            self._policy.maximum_resource_high_watermark
        ):
            reasons.append("RESOURCE_HIGH_WATERMARK_EXCEEDED")
        return ReadinessGate(
            "capacity",
            GateDecision.FAIL if reasons else GateDecision.PASS,
            tuple(reasons),
            (evidence.reference,),
        )

    def _evaluation_reason(self, report: EvaluationReport | None, now: datetime) -> str | None:
        if report is None:
            return "EVALUATION_MISSING"
        if not self._safe_reference(report.metadata.comparison_key):
            return "EVALUATION_REFERENCE_INVALID"
        try:
            executed_at = datetime.fromisoformat(report.metadata.executed_at)
        except ValueError:
            return "EVALUATION_TIME_INVALID"
        if self._stale(executed_at, now):
            return "EVALUATION_STALE"
        identity = self._inputs.identity
        route_fingerprints = frozenset(
            f"{route.data_classification.value}:{route.configuration_fingerprint}"
            for route in report.metadata.routes
        )
        if any(not route.production_ready for route in report.metadata.routes):
            return "EVALUATION_ROUTE_NOT_PRODUCTION_READY"
        if (
            report.metadata.dataset_version != identity.dataset_version
            or report.metadata.dataset_hash != identity.dataset_hash
            or report.metadata.index_identity != identity.index_identity
            or report.metadata.index_build_hash != identity.index_build_hash
            or report.metadata.parser_version != identity.parser_version
            or report.metadata.chunk_version != identity.chunk_version
            or report.metadata.configuration_fingerprint != identity.configuration_fingerprint
            or route_fingerprints != identity.route_fingerprints
        ):
            return "EVALUATION_IDENTITY_MISMATCH"
        return None

    @staticmethod
    def _valid_ratio(value: float) -> bool:
        return isfinite(value) and 0 <= value <= 1

    @staticmethod
    def _safe_reference(reference: str) -> bool:
        if SAFE_OPAQUE_REFERENCE.fullmatch(reference) is None:
            return False
        if "://" not in reference:
            return True
        parsed = urlsplit(reference)
        return not any((parsed.username, parsed.password, parsed.query, parsed.fragment))

    def _common_evidence_reason(
        self, observed_at: datetime, identity_fingerprint: str, now: datetime
    ) -> str | None:
        if identity_fingerprint != self._inputs.identity.fingerprint:
            return "EVIDENCE_IDENTITY_MISMATCH"
        if self._stale(observed_at, now):
            return "EVIDENCE_STALE"
        return None

    def _stale(self, observed_at: datetime, now: datetime) -> bool:
        if observed_at.tzinfo is None:
            return True
        age = now - observed_at
        return age < timedelta(0) or age > self._policy.maximum_evidence_age

    @staticmethod
    def _aggregate(decisions: Iterable[GateDecision]) -> GateDecision:
        values = tuple(decisions)
        if GateDecision.FAIL in values:
            return GateDecision.FAIL
        if GateDecision.INDETERMINATE in values:
            return GateDecision.INDETERMINATE
        return GateDecision.PASS

    @staticmethod
    def _gate(name: str, decision: GateDecision, reason: str) -> ReadinessGate:
        return ReadinessGate(name, decision, (reason,))
