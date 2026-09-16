import json
import logging
from datetime import datetime, timedelta
from queue import Full, Queue
from threading import Condition, Lock, Thread
from time import monotonic
from typing import Any, cast
from uuid import UUID

from redis import Redis

from erp_assistant.operations.readiness_models import RuntimeEvidence
from erp_assistant.operations.telemetry import (
    ALLOWED_OUTCOMES,
    ALLOWED_SIGNALS,
    ALLOWED_STAGES,
    SAFE_ATTRIBUTE_NAMES,
    SAFE_LABEL,
    TelemetryEvent,
    TelemetryValue,
    runtime_evidence_from_events,
)

logger = logging.getLogger(__name__)


class RedisTelemetryStore:
    """Shared bounded event buffer for cross-process release evidence."""

    def __init__(
        self,
        redis_url: str,
        *,
        max_events: int = 50_000,
        retention: timedelta = timedelta(days=2),
        queue_capacity: int = 2_000,
    ) -> None:
        self._redis = Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        self._max_events = max_events
        self._retention_seconds = int(retention.total_seconds())
        self._queue: Queue[TelemetryEvent | None] = Queue(maxsize=queue_capacity)
        self._failure_lock = Lock()
        self._pending_failures = 0
        self._state = Condition()
        self._inflight = 0
        self._closed = False
        self._worker = Thread(target=self._drain, daemon=True, name="telemetry-redis")
        self._worker.start()

    def export(self, event: TelemetryEvent) -> None:
        with self._state:
            if self._closed:
                self._record_failure()
                raise RuntimeError("Shared telemetry store is closed")
            self._inflight += 1
            try:
                self._queue.put_nowait(event)
            except Full as exc:
                self._inflight -= 1
                self._state.notify_all()
                self._record_failure()
                raise RuntimeError("Shared telemetry buffer is full") from exc

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait only up to the evidence deadline for queued exports."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = monotonic() + timeout
        with self._state:
            while self._inflight:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._record_failure()
                    return False
                self._state.wait(remaining)
        return True

    def close(self, timeout: float = 5.0) -> bool:
        with self._state:
            if self._closed:
                return self._inflight == 0
            self._closed = True
        flushed = self.flush(timeout)
        if flushed:
            self._queue.put_nowait(None)
        return flushed

    def _drain(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                self._queue.task_done()
                return
            try:
                self._write(event)
            except Exception:
                self._record_failure()
                logger.warning("shared_telemetry_export_failed")
            finally:
                self._queue.task_done()
                with self._state:
                    self._inflight -= 1
                    self._state.notify_all()

    def _write(self, event: TelemetryEvent) -> None:
        key = self._key(event.release_fingerprint)
        counter_key = self._counter_key(event.release_fingerprint)
        with self._failure_lock:
            pending_failures = self._pending_failures
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.lpush(key, json.dumps(event.to_dict(), sort_keys=True))
        pipeline.ltrim(key, 0, self._max_events - 1)
        pipeline.expire(key, self._retention_seconds)
        if pending_failures:
            pipeline.hincrby(counter_key, "exporterFailures", pending_failures)
            pipeline.hincrby(counter_key, "exporterAlerts", pending_failures)
            pipeline.expire(counter_key, self._retention_seconds)
        pipeline.execute()
        if pending_failures:
            with self._failure_lock:
                self._pending_failures -= pending_failures

    def evidence(
        self,
        *,
        identity_fingerprint: str,
        window_end: datetime,
        window: timedelta = timedelta(hours=24),
    ) -> RuntimeEvidence:
        self.flush()
        self._persist_pending_failures(identity_fingerprint)
        raw_events = cast(list[str], self._redis.lrange(self._key(identity_fingerprint), 0, -1))
        events = tuple(
            event
            for raw in raw_events
            if (event := self._decode(raw, identity_fingerprint)) is not None
        )
        counters = cast(
            dict[str, Any],
            self._redis.hgetall(self._counter_key(identity_fingerprint)),
        )
        return runtime_evidence_from_events(
            events,
            identity_fingerprint=identity_fingerprint,
            window_end=window_end,
            window=window,
            exporter_failure_count=int(counters.get("exporterFailures", 0)),
            exporter_alert_count=int(counters.get("exporterAlerts", 0)),
            reference=f"redis://telemetry/{identity_fingerprint}",
        )

    def _persist_pending_failures(self, identity_fingerprint: str) -> None:
        with self._failure_lock:
            pending_failures = self._pending_failures
        if not pending_failures:
            return
        counter_key = self._counter_key(identity_fingerprint)
        pipeline = self._redis.pipeline(transaction=False)
        pipeline.hincrby(counter_key, "exporterFailures", pending_failures)
        pipeline.hincrby(counter_key, "exporterAlerts", pending_failures)
        pipeline.expire(counter_key, self._retention_seconds)
        pipeline.execute()
        with self._failure_lock:
            self._pending_failures -= pending_failures

    def _record_failure(self) -> None:
        with self._failure_lock:
            self._pending_failures += 1

    @staticmethod
    def _key(identity_fingerprint: str) -> str:
        if SAFE_LABEL.fullmatch(identity_fingerprint) is None:
            raise ValueError("Unsafe release fingerprint")
        return f"erp_assistant:telemetry:{identity_fingerprint}"

    @classmethod
    def _counter_key(cls, identity_fingerprint: str) -> str:
        return cls._key(identity_fingerprint) + ":counters"

    @staticmethod
    def _decode(raw: str, expected_fingerprint: str) -> TelemetryEvent | None:
        try:
            payload = json.loads(raw)
            trace_id = UUID(payload["traceId"])
            fingerprint = str(payload["releaseFingerprint"])
            signal = str(payload["signal"])
            stage = str(payload["stage"])
            outcome = str(payload["outcome"])
            occurred_at = datetime.fromisoformat(payload["occurredAt"])
            unsafe_count = int(payload.get("unsafeAttributeCount", 0))
            raw_attributes = dict(payload["attributes"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            fingerprint != expected_fingerprint
            or occurred_at.tzinfo is None
            or signal not in ALLOWED_SIGNALS
            or stage not in ALLOWED_STAGES
            or outcome not in ALLOWED_OUTCOMES
            or unsafe_count < 0
        ):
            return None
        attributes: list[tuple[str, TelemetryValue]] = []
        for name, value in raw_attributes.items():
            if (
                name not in SAFE_ATTRIBUTE_NAMES
                or not isinstance(value, (str, int, float, bool))
                or (isinstance(value, str) and SAFE_LABEL.fullmatch(value) is None)
            ):
                return None
            attributes.append((name, value))
        return TelemetryEvent(
            trace_id=trace_id,
            release_fingerprint=fingerprint,
            signal=signal,
            stage=stage,
            outcome=outcome,
            attributes=tuple(sorted(attributes)),
            occurred_at=occurred_at,
            unsafe_attribute_count=unsafe_count,
        )
