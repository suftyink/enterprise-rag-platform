import asyncio
from uuid import UUID, uuid4

from erp_assistant.core.config import get_settings
from erp_assistant.infrastructure.database import SessionFactory, engine
from erp_assistant.operations.initialization_worker import (
    execute_initialization_build,
    sweep_stale_initialization_runs,
)
from erp_assistant.operations.queue_monitor import SqlIndexQueueMonitor
from erp_assistant.operations.telemetry import OperationalTelemetry, get_telemetry
from erp_assistant.rag.cleanup_executor import CleanupRetryableError, KnowledgeCleanupExecutor
from erp_assistant.rag.composition import get_cleanup_executor, get_knowledge_corpus
from erp_assistant.rag.document_indexer import DocumentIndexRetryableError
from erp_assistant.rag.knowledge_corpus import (
    SYSTEM_CORPUS_ACTOR,
    KnowledgeCorpus,
)
from erp_assistant.workers.celery_app import celery_app


@celery_app.task(name="erp_assistant.health")  # type: ignore[untyped-decorator]
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _run_index(
    document_id: UUID,
    trace_id: UUID | None = None,
    corpus: KnowledgeCorpus | None = None,
    telemetry: OperationalTelemetry | None = None,
) -> dict[str, object]:
    operation_trace = trace_id or uuid4()
    signals = telemetry or get_telemetry()
    try:
        view = await (corpus or get_knowledge_corpus()).submit(
            document_id,
            SYSTEM_CORPUS_ACTOR,
            reason="index pipeline completed",
            trace_id=operation_trace,
        )
        signals.emit(
            trace_id=operation_trace,
            signal="indexing",
            stage="indexing",
            outcome="SUCCEEDED",
            attributes={
                "chunk_count": view.chunk_summary.chunk_count,
                "decision": view.processing_status.value,
                "document_version": view.version,
            },
        )
        return view.to_dict()
    except Exception as exc:
        signals.emit(
            trace_id=operation_trace,
            signal="indexing",
            stage="indexing",
            outcome="FAILED",
            attributes={"error_code": exc.__class__.__name__},
        )
        raise
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="erp_assistant.index_document",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(DocumentIndexRetryableError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def index_document(task: object, document_id: str) -> dict[str, object]:
    return asyncio.run(_run_index(UUID(document_id), _task_trace_id(task)))


def _task_trace_id(task: object) -> UUID:
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", None) or {}
    raw_trace_id = headers.get("trace_id")
    if isinstance(raw_trace_id, str):
        try:
            return UUID(raw_trace_id)
        except ValueError:
            pass
    return uuid4()


async def _run_queue_observation(trace_id: UUID) -> dict[str, int]:
    try:
        snapshot = await SqlIndexQueueMonitor(
            SessionFactory,
            get_telemetry(),
        ).observe(trace_id=trace_id)
        return {
            "queueDepth": snapshot.queue_depth,
            "retryCount": snapshot.retry_count,
            "failedJobCount": snapshot.failed_job_count,
            "stuckJobCount": snapshot.stuck_job_count,
        }
    finally:
        await engine.dispose()


@celery_app.task(name="erp_assistant.observe_index_queue")  # type: ignore[untyped-decorator]
def observe_index_queue() -> dict[str, int]:
    return asyncio.run(_run_queue_observation(uuid4()))


async def _run_cleanup(
    document_id: UUID,
    executor: KnowledgeCleanupExecutor | None = None,
) -> dict[str, object]:
    try:
        outcome = await (executor or get_cleanup_executor()).execute(document_id)
        return outcome.to_dict()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="erp_assistant.cleanup_knowledge_document",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(CleanupRetryableError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def cleanup_knowledge_document(document_id: str) -> dict[str, object]:
    return asyncio.run(_run_cleanup(UUID(document_id)))


async def _run_initialization_build(
    run_id: UUID, *, lease_token: str | None = None
) -> dict[str, object]:
    try:
        outcome = await execute_initialization_build(
            run_id,
            SessionFactory,
            get_settings(),
            lease_token=lease_token,
        )
        return outcome.to_dict()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="erp_assistant.build_knowledge_initialization",
    acks_late=True,
    reject_on_worker_lost=True,
)
def build_knowledge_initialization(task: object, run_id: str) -> dict[str, object]:
    request = getattr(task, "request", None)
    task_id = getattr(request, "id", None)
    lease_token = f"task:{task_id}" if task_id else None
    return asyncio.run(
        _run_initialization_build(UUID(run_id), lease_token=lease_token)
    )


async def _run_initialization_sweep() -> dict[str, int]:
    try:
        count = await sweep_stale_initialization_runs(SessionFactory)
        return {"expired": count}
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="erp_assistant.sweep_knowledge_initializations"
)
def sweep_knowledge_initializations() -> dict[str, int]:
    return asyncio.run(_run_initialization_sweep())
