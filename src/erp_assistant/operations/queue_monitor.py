from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.knowledge import KnowledgeIndexJob
from erp_assistant.operations.contracts import Telemetry
from erp_assistant.operations.queue_signals import (
    IndexQueueSnapshot,
    emit_index_queue_signals,
)


class SqlIndexQueueMonitor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        telemetry: Telemetry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._telemetry = telemetry
        self._clock = clock

    async def observe(self, *, trace_id: UUID) -> IndexQueueSnapshot:
        now = self._clock()
        async with self._sessions() as session:
            latest = select(
                KnowledgeIndexJob.status.label("status"),
                KnowledgeIndexJob.attempt.label("attempt"),
                KnowledgeIndexJob.lease_expires_at.label("lease_expires_at"),
                func.row_number()
                .over(
                    partition_by=KnowledgeIndexJob.document_id,
                    order_by=(
                        KnowledgeIndexJob.created_at.desc(),
                        KnowledgeIndexJob.id.desc(),
                    ),
                )
                .label("job_rank"),
            ).subquery()
            row = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case((latest.c.status == "QUEUED", 1), else_=0)
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case((latest.c.attempt > 1, latest.c.attempt - 1), else_=0)
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case((latest.c.status == "FAILED", 1), else_=0)
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        (latest.c.status == "RUNNING")
                                        & latest.c.lease_expires_at.is_not(None)
                                        & (latest.c.lease_expires_at < now),
                                        1,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                    ).where(latest.c.job_rank == 1)
                )
            ).one()
        snapshot = IndexQueueSnapshot(
            queue_depth=int(row[0]),
            retry_count=int(row[1]),
            failed_job_count=int(row[2]),
            stuck_job_count=int(row[3]),
        )
        emit_index_queue_signals(self._telemetry, trace_id=trace_id, snapshot=snapshot)
        return snapshot
