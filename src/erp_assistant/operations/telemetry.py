import json
import logging
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from threading import Lock
from typing import Protocol
from uuid import UUID

from erp_assistant.operations.readiness_models import RuntimeEvidence

logger = logging.getLogger(__name__)

TelemetryValue = str | int | float | bool
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_:\-]{0,127}$")
SECRET_LIKE = re.compile(r"(?i)^(?:bearer|sk-|eyJ|token|secret|password|credential)")
ALLOWED_SIGNALS = frozenset(
    {
        "answer",
        "authorization_denial",
        "embedding",
        "failed_job",
        "filtering",
        "http_request",
        "indexing",
        "lifecycle_transition",
        "model_generation",
        "parsing",
        "queue_depth",
        "recall",
        "rerank",
        "retrieval",
        "retrieval_gate",
        "retry_count",
        "stuck_job",
    }
)
ALLOWED_STAGES = ALLOWED_SIGNALS | frozenset(
    {"authorization", "http", "index_queue", "lifecycle"}
)
ALLOWED_OUTCOMES = frozenset({"DEGRADED", "DENIED", "FAILED", "OBSERVED", "SUCCEEDED"})

SAFE_ATTRIBUTE_NAMES = frozenset(
    {
        "duration_ms",
        "candidate_count",
        "chunk_count",
        "document_version",
        "parser_version",
        "chunk_version",
        "index_identity",
        "model",
        "decision",
        "reason_code",
        "error_code",
        "queue_depth",
        "retry_count",
        "failed_job_count",
        "stuck_job_count",
        "status",
        "phase",
        "profile_version",
    }
)


@dataclass(frozen=True)
class TelemetryEvent:
    trace_id: UUID
    release_fingerprint: str
    signal: str
    stage: str
    outcome: str
    attributes: tuple[tuple[str, TelemetryValue], ...]
    occurred_at: datetime
    unsafe_attribute_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "traceId": str(self.trace_id),
            "releaseFingerprint": self.release_fingerprint,
            "signal": self.signal,
            "stage": self.stage,
            "outcome": self.outcome,
            "attributes": dict(self.attributes),
            "occurredAt": self.occurred_at.isoformat(),
            "unsafeAttributeCount": self.unsafe_attribute_count,
        }


class TelemetryExporter(Protocol):
    def export(self, event: TelemetryEvent) -> None: ...


@dataclass
class RecordingTelemetryExporter:
    events: list[TelemetryEvent] = field(default_factory=list)

    def export(self, event: TelemetryEvent) -> None:
        self.events.append(event)


class LoggingTelemetryExporter:
    def export(self, event: TelemetryEvent) -> None:
        logger.info("operational_telemetry %s", json.dumps(event.to_dict(), sort_keys=True))


class OperationalTelemetry:
    """Best-effort telemetry that exports only explicitly approved scalar fields."""

    def __init__(
        self,
        exporters: Iterable[TelemetryExporter] = (),
        *,
        max_events: int = 10_000,
        release_fingerprint: str = "UNBOUND",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._exporters = tuple(exporters)
        self._events: deque[TelemetryEvent] = deque(maxlen=max_events)
        self._exporter_failure_count = 0
        self._exporter_alert_count = 0
        self._lock = Lock()
        self._clock = clock
        self._release_fingerprint = release_fingerprint

    def emit(
        self,
        *,
        trace_id: UUID,
        signal: str,
        stage: str,
        outcome: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        values = attributes or {}
        safe: list[tuple[str, TelemetryValue]] = []
        dropped = 0
        for name, value in values.items():
            if (
                name in SAFE_ATTRIBUTE_NAMES
                and isinstance(value, (str, int, float, bool))
                and (not isinstance(value, str) or bool(value))
            ):
                safe_value = _safe_attribute_value(name, value)
                if safe_value is None:
                    dropped += 1
                else:
                    safe.append((name, safe_value))
            else:
                dropped += 1
        safe_signal, invalid_signal = _safe_event_label(
            signal, "invalid_signal", ALLOWED_SIGNALS
        )
        safe_stage, invalid_stage = _safe_event_label(
            stage, "invalid_stage", ALLOWED_STAGES
        )
        safe_outcome, invalid_outcome = _safe_event_label(
            outcome, "INVALID", ALLOWED_OUTCOMES
        )
        dropped += invalid_signal + invalid_stage + invalid_outcome
        event = TelemetryEvent(
            trace_id=trace_id,
            release_fingerprint=self._release_fingerprint,
            signal=safe_signal,
            stage=safe_stage,
            outcome=safe_outcome,
            attributes=tuple(sorted(safe)),
            occurred_at=self._clock(),
            unsafe_attribute_count=dropped,
        )
        with self._lock:
            self._events.append(event)
        for exporter in self._exporters:
            try:
                exporter.export(event)
            except Exception:
                with self._lock:
                    self._exporter_failure_count += 1
                    self._exporter_alert_count += 1
                logger.warning(
                    "telemetry_export_failed trace_id=%s exporter=%s",
                    trace_id,
                    exporter.__class__.__name__,
                )

    def snapshot(
        self,
        *,
        window: timedelta = timedelta(hours=24),
    ) -> RuntimeEvidence:
        if window <= timedelta(0):
            raise ValueError("window must be positive")
        with self._lock:
            events = tuple(self._events)
            failure_count = self._exporter_failure_count
            alert_count = self._exporter_alert_count
        return runtime_evidence_from_events(
            events,
            identity_fingerprint=self._release_fingerprint,
            window_end=self._clock(),
            window=window,
            exporter_failure_count=failure_count,
            exporter_alert_count=alert_count,
            unsafe_attribute_count=0,
        )

    def close(self, timeout: float = 5.0) -> None:
        """Flush shared exporters without allowing shutdown to block indefinitely."""
        for exporter in self._exporters:
            close = getattr(exporter, "close", None)
            if close is None:
                continue
            try:
                if close(timeout) is False:
                    with self._lock:
                        self._exporter_failure_count += 1
                        self._exporter_alert_count += 1
            except Exception:
                with self._lock:
                    self._exporter_failure_count += 1
                    self._exporter_alert_count += 1
                logger.warning(
                    "telemetry_exporter_close_failed exporter=%s",
                    exporter.__class__.__name__,
                )


def runtime_evidence_from_events(
    events: Iterable[TelemetryEvent],
    *,
    identity_fingerprint: str,
    window_end: datetime,
    window: timedelta,
    exporter_failure_count: int = 0,
    exporter_alert_count: int = 0,
    unsafe_attribute_count: int = 0,
    reference: str = "runtime://memory",
) -> RuntimeEvidence:
    """Aggregate a bounded window, including events collected across processes."""
    window_start = window_end - window
    selected = tuple(
        event
        for event in events
        if event.release_fingerprint == identity_fingerprint
        and window_start <= event.occurred_at <= window_end
    )
    signals_by_trace: dict[UUID, set[str]] = defaultdict(set)
    for event in selected:
        signals_by_trace[event.trace_id].add(event.signal)
    observed_at = max(
        (event.occurred_at for event in selected),
        default=datetime.min.replace(tzinfo=UTC),
    )
    return RuntimeEvidence(
        observed_at=observed_at,
        identity_fingerprint=identity_fingerprint,
        signals=frozenset(event.signal for event in selected),
        exporter_failure_count=exporter_failure_count,
        exporter_alert_count=exporter_alert_count,
        unsafe_attribute_count=(
            unsafe_attribute_count
            + sum(event.unsafe_attribute_count for event in selected)
        ),
        trace_continuity_count=sum(
            1 for signals in signals_by_trace.values() if len(signals) > 1
        ),
        failed_signals=frozenset(
            event.signal for event in selected if event.outcome == "FAILED"
        ),
        trace_chains=_trace_chains(signals_by_trace),
        reference=reference,
    )


def _safe_event_label(
    value: str,
    fallback: str,
    allowed: frozenset[str],
) -> tuple[str, int]:
    if value in allowed:
        return value, 0
    return fallback, 1


def _safe_attribute_value(name: str, value: TelemetryValue) -> TelemetryValue | None:
    if not isinstance(value, str):
        return value
    if name in {"decision", "error_code", "phase", "reason_code", "status"}:
        return value if SAFE_CODE.fullmatch(value) is not None else None
    if SAFE_LABEL.fullmatch(value) is not None and SECRET_LIKE.search(value) is None:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"sha256:{digest}"


def _trace_chains(signals_by_trace: Mapping[UUID, set[str]]) -> frozenset[str]:
    chains = {
        "indexing": {"http_request", "parsing", "indexing", "lifecycle_transition"},
        "retrieval": {"embedding", "recall", "filtering", "rerank", "retrieval_gate"},
        "answer": {"retrieval", "model_generation", "answer"},
    }
    return frozenset(
        name
        for name, required in chains.items()
        if any(required <= signals for signals in signals_by_trace.values())
    )


@lru_cache
def get_telemetry() -> OperationalTelemetry:
    from erp_assistant.core.config import get_settings

    settings = get_settings()
    fingerprint = settings.pilot_release_fingerprint or "UNBOUND"
    exporters: list[TelemetryExporter] = [LoggingTelemetryExporter()]
    if settings.pilot_release_fingerprint:
        from erp_assistant.operations.telemetry_redis import RedisTelemetryStore

        exporters.append(RedisTelemetryStore(settings.redis_url))
    return OperationalTelemetry(
        exporters,
        release_fingerprint=fingerprint,
    )
