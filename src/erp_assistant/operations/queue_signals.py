from dataclasses import dataclass
from uuid import UUID

from erp_assistant.operations.contracts import Telemetry


@dataclass(frozen=True)
class IndexQueueSnapshot:
    queue_depth: int
    retry_count: int
    failed_job_count: int
    stuck_job_count: int


def emit_index_queue_signals(
    telemetry: Telemetry,
    *,
    trace_id: UUID,
    snapshot: IndexQueueSnapshot,
) -> None:
    values = {
        "queue_depth": snapshot.queue_depth,
        "retry_count": snapshot.retry_count,
        "failed_job_count": snapshot.failed_job_count,
        "stuck_job_count": snapshot.stuck_job_count,
    }
    telemetry.emit(
        trace_id=trace_id,
        signal="queue_depth",
        stage="index_queue",
        outcome="OBSERVED",
        attributes=values,
    )
    telemetry.emit(
        trace_id=trace_id,
        signal="retry_count",
        stage="index_queue",
        outcome="DEGRADED" if snapshot.retry_count else "OBSERVED",
        attributes=values,
    )
    telemetry.emit(
        trace_id=trace_id,
        signal="failed_job",
        stage="index_queue",
        outcome="FAILED" if snapshot.failed_job_count else "OBSERVED",
        attributes=values,
    )
    telemetry.emit(
        trace_id=trace_id,
        signal="stuck_job",
        stage="index_queue",
        outcome="FAILED" if snapshot.stuck_job_count else "OBSERVED",
        attributes=values,
    )
