from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.core.config import Settings
from erp_assistant.models.operations import (
    CorpusReleaseRecord,
    KnowledgeInitializationAuditRecord,
    KnowledgeInitializationRunRecord,
)
from erp_assistant.operations.initialization_contracts import (
    InitializationError,
    InitializationRunView,
    InitializationStatus,
    view,
)
from erp_assistant.operations.release_contracts import ReleaseStatus
from erp_assistant.operations.releases import CorpusReleaseWorkflow
from erp_assistant.rag.errors import RagError


async def execute_initialization_build(
    run_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    lease_token: str | None = None,
) -> InitializationRunView:
    from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
    from erp_assistant.rag.staging_index import build_staging_index

    async with session_factory() as session:
        run = await session.scalar(
            select(KnowledgeInitializationRunRecord)
            .where(KnowledgeInitializationRunRecord.id == run_id)
            .with_for_update()
        )
        if run is None:
            raise InitializationError("INITIALIZATION_NOT_FOUND", "初始化运行不存在")
        if run.status == InitializationStatus.CANCELLED.value:
            return view(run)
        if (
            run.candidate_release_id is None
            or run.profile_id is None
            or run.status != InitializationStatus.QUEUED.value
        ):
            raise InitializationError(
                "INITIALIZATION_BUILD_NOT_AVAILABLE", "当前初始化运行没有可构建候选"
            )
        observed_at = datetime.now(UTC)
        expires_at = run.lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            lease_token is None
            or run.lease_owner != lease_token
            or expires_at is None
            or expires_at <= observed_at
        ):
            raise InitializationError(
                "INITIALIZATION_BUILD_LEASE_REJECTED",
                "候选构建任务租约无效或已过期",
                retryable=True,
            )
        release = await session.get(CorpusReleaseRecord, run.candidate_release_id)
        if release is None:
            raise InitializationError("RELEASE_NOT_FOUND", "候选 Release 不存在")
        release.status = ReleaseStatus.BUILDING.value
        release.failure_reason_code = None
        run.status = InitializationStatus.RUNNING.value
        run.failure_code = None
        run.failure_message = None
        run.retryable = False
        run.lease_owner = lease_token
        run.lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=max(settings.knowledge_index_lease_seconds, 1)
        )
        run.progress = 75
        run.revision += 1
        run.updated_at = datetime.now(UTC)
        profile_id = run.profile_id
        release_id = run.candidate_release_id
        trace_id = run.trace_id
        actor_id = run.actor_id
        lease_owner = run.lease_owner
        claimed_attempt = run.attempt
        claimed_revision = run.revision
        await session.commit()

    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_initialization_build(
            run_id,
            lease_owner=lease_owner,
            claimed_attempt=claimed_attempt,
            claimed_revision=claimed_revision,
            session_factory=session_factory,
            lease_seconds=settings.knowledge_index_lease_seconds,
            stop=heartbeat_stop,
        )
    )
    try:
        registry = build_retrieval_runtime_registry(settings)
        await build_staging_index(
            profile_id,
            registry,
            session_factory,
            release_id=release_id,
        )
        async with session_factory() as session:
            run = await session.scalar(
                select(KnowledgeInitializationRunRecord)
                .where(KnowledgeInitializationRunRecord.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise InitializationError(
                    "INITIALIZATION_NOT_FOUND", "初始化运行不存在"
                )
            if run.status == InitializationStatus.CANCELLED.value:
                return view(run)
            if not _owns_claim(
                run,
                lease_owner=lease_owner,
                attempt=claimed_attempt,
                revision=claimed_revision,
            ):
                return view(run)
            await CorpusReleaseWorkflow(session).sync_build(
                release_id,
                actor_id=actor_id,
                trace_id=trace_id,
                commit=False,
            )
            run.status = InitializationStatus.SUCCEEDED.value
            run.stage = "EVALUATION_HANDOFF"
            run.progress = 100
            run.lease_owner = None
            run.lease_expires_at = None
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            session.add(
                KnowledgeInitializationAuditRecord(
                    id=uuid4(),
                    run_id=run.id,
                    action="BUILD",
                    actor_id=actor_id,
                    reason_code="CANDIDATE_READY_FOR_EVALUATION",
                    trace_id=trace_id,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return view(run)
    except Exception as exc:
        async with session_factory() as session:
            run = await session.scalar(
                select(KnowledgeInitializationRunRecord)
                .where(KnowledgeInitializationRunRecord.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise
            if run.status == InitializationStatus.CANCELLED.value:
                return view(run)
            if not _owns_claim(
                run,
                lease_owner=lease_owner,
                attempt=claimed_attempt,
                revision=claimed_revision,
            ):
                return view(run)
            failure_code, failure_message, retryable = _safe_failure(exc)
            run.status = InitializationStatus.FAILED.value
            run.failure_code = failure_code
            run.failure_message = failure_message
            run.retryable = retryable
            run.lease_owner = None
            run.lease_expires_at = None
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            session.add(
                KnowledgeInitializationAuditRecord(
                    id=uuid4(),
                    run_id=run.id,
                    action="FAIL",
                    actor_id=actor_id,
                    reason_code=run.failure_code,
                    trace_id=trace_id,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return view(run)
    finally:
        heartbeat_stop.set()
        await heartbeat


async def _heartbeat_initialization_build(
    run_id: UUID,
    *,
    lease_owner: str,
    claimed_attempt: int,
    claimed_revision: int,
    session_factory: async_sessionmaker[AsyncSession],
    lease_seconds: int,
    stop: asyncio.Event,
) -> None:
    interval = max(1.0, min(float(lease_seconds) / 3.0, 30.0))
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        async with session_factory() as session:
            run = await session.scalar(
                select(KnowledgeInitializationRunRecord)
                .where(KnowledgeInitializationRunRecord.id == run_id)
                .with_for_update()
            )
            if (
                run is None
                or run.status != InitializationStatus.RUNNING.value
                or run.stage != "CANDIDATE_BUILD"
                or run.lease_owner != lease_owner
                or run.attempt != claimed_attempt
                or run.revision != claimed_revision
            ):
                return
            run.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=max(lease_seconds, 1)
            )
            run.updated_at = datetime.now(UTC)
            await session.commit()


def _owns_claim(
    run: KnowledgeInitializationRunRecord,
    *,
    lease_owner: str,
    attempt: int,
    revision: int,
) -> bool:
    return bool(
        run.status == InitializationStatus.RUNNING.value
        and run.stage == "CANDIDATE_BUILD"
        and run.lease_owner == lease_owner
        and run.attempt == attempt
        and run.revision == revision
    )


def _safe_failure(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, InitializationError):
        return exc.code, exc.message, exc.retryable
    if isinstance(exc, RagError):
        return exc.code, exc.message, True
    if isinstance(exc, (SQLAlchemyError, ConnectionError, TimeoutError, OSError)):
        return (
            "INITIALIZATION_BUILD_DEPENDENCY_UNAVAILABLE",
            "候选索引构建依赖暂时不可用",
            True,
        )
    return "CANDIDATE_BUILD_FAILED", "候选索引构建失败", False


async def sweep_stale_initialization_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> int:
    observed_at = now or datetime.now(UTC)
    count = 0
    async with session_factory() as session:
        runs = tuple(
            (
                await session.scalars(
                    select(KnowledgeInitializationRunRecord)
                    .where(
                        KnowledgeInitializationRunRecord.status.in_(
                            [
                                InitializationStatus.QUEUED.value,
                                InitializationStatus.RUNNING.value,
                            ]
                        ),
                        KnowledgeInitializationRunRecord.stage == "CANDIDATE_BUILD",
                        KnowledgeInitializationRunRecord.lease_expires_at.is_not(None),
                        KnowledgeInitializationRunRecord.lease_expires_at <= observed_at,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for run in runs:
            queued = run.status == InitializationStatus.QUEUED.value
            run.status = InitializationStatus.FAILED.value
            run.failure_code = (
                "INITIALIZATION_DISPATCH_EXPIRED"
                if queued
                else "INITIALIZATION_WORKER_LEASE_EXPIRED"
            )
            run.failure_message = (
                "候选构建任务未在期限内被 Worker 领取"
                if queued
                else "候选构建 Worker 心跳已过期"
            )
            run.retryable = True
            run.lease_owner = None
            run.lease_expires_at = None
            run.revision += 1
            run.updated_at = observed_at
            session.add(
                KnowledgeInitializationAuditRecord(
                    id=uuid4(),
                    run_id=run.id,
                    action="FAIL",
                    actor_id=run.actor_id,
                    reason_code=run.failure_code,
                    trace_id=run.trace_id,
                    created_at=observed_at,
                )
            )
            count += 1
        if count:
            await session.commit()
    return count
