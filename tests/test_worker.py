from uuid import uuid4

import pytest

import erp_assistant.workers.tasks as worker_tasks
from erp_assistant.operations.telemetry import OperationalTelemetry, RecordingTelemetryExporter
from erp_assistant.rag.cleanup_executor import CleanupDecision, CleanupOutcome
from erp_assistant.rag.knowledge_corpus import (
    SYSTEM_CORPUS_ACTOR,
    CorpusChunkSummary,
    CorpusDocumentView,
)
from erp_assistant.rag.types import DocumentStatus, PublicationStatus, RetentionStatus
from erp_assistant.workers.celery_app import celery_app
from erp_assistant.workers.dispatcher import enqueue_document
from erp_assistant.workers.tasks import (
    _run_cleanup,
    _run_index,
    _task_trace_id,
    cleanup_knowledge_document,
    health,
    index_document,
)


class StubCorpus:
    def __init__(self, view: CorpusDocumentView) -> None:
        self.view = view
        self.calls: list[tuple[object, object, str, object]] = []

    async def submit(
        self, document_id: object, actor: object, *, reason: str, trace_id: object
    ) -> CorpusDocumentView:
        self.calls.append((document_id, actor, reason, trace_id))
        return self.view


class StubCleanupExecutor:
    def __init__(self, outcome: CleanupOutcome) -> None:
        self.outcome = outcome
        self.calls: list[object] = []

    async def execute(self, document_id: object) -> CleanupOutcome:
        self.calls.append(document_id)
        return self.outcome


def test_health_task_is_registered_and_side_effect_free() -> None:
    celery_app.finalize()
    assert "erp_assistant.health" in celery_app.tasks
    assert "erp_assistant.observe_index_queue" in celery_app.tasks
    assert celery_app.conf.beat_schedule["observe-index-queue-every-minute"] == {
        "task": "erp_assistant.observe_index_queue",
        "schedule": 60.0,
    }
    assert health.run() == {"status": "ok"}


def test_index_worker_acknowledges_only_after_persisted_outcome() -> None:
    assert index_document.acks_late is True
    assert index_document.reject_on_worker_lost is True
    assert cleanup_knowledge_document.acks_late is True
    assert cleanup_knowledge_document.reject_on_worker_lost is True


def test_dispatch_keeps_one_argument_envelope_and_carries_trace_in_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_id = uuid4()
    trace_id = uuid4()
    captured: dict[str, object] = {}

    class Result:
        id = "task-9"

    class Task:
        def apply_async(self, *, args: list[str], headers: dict[str, str]) -> Result:
            captured.update(args=args, headers=headers)
            return Result()

    monkeypatch.setattr(worker_tasks, "index_document", Task())

    task_id = enqueue_document(document_id, trace_id)

    assert task_id == "task-9"
    assert captured == {
        "args": [str(document_id)],
        "headers": {"trace_id": str(trace_id)},
    }


def test_legacy_or_invalid_trace_header_falls_back_to_a_server_trace() -> None:
    class Request:
        headers = {"trace_id": "not-a-uuid"}

    class Task:
        request = Request()

    assert isinstance(_task_trace_id(Task()), type(uuid4()))


@pytest.mark.asyncio
async def test_index_worker_returns_the_corpus_outcome() -> None:
    document_id = uuid4()
    trace_id = uuid4()
    view = CorpusDocumentView(
        document_id=document_id,
        document_key="inventory-sop",
        version=1,
        title="Inventory SOP",
        original_filename="inventory.pdf",
        media_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        processing_status=DocumentStatus.READY,
        publication_status=PublicationStatus.REVIEW_PENDING,
        retention_status=RetentionStatus.RETAINED,
        revision=0,
        retrieval_eligible=False,
        warnings=("PAGE_1_NO_TEXT",),
        pipeline_revision="a" * 64,
        parser_version="pypdf-v1",
        chunking_version="structured-window-v1",
        quality_signals={"qualityScore": 0.95},
        chunk_summary=CorpusChunkSummary(
            chunk_count=0,
            character_count=0,
            first_page=None,
            last_page=None,
        ),
        audit_events=(),
        cleanup_requests=(),
    )
    corpus = StubCorpus(view)
    exporter = RecordingTelemetryExporter()
    telemetry = OperationalTelemetry((exporter,))

    result = await _run_index(  # type: ignore[arg-type]
        document_id,
        trace_id,
        corpus,
        telemetry,
    )

    assert corpus.calls == [
        (document_id, SYSTEM_CORPUS_ACTOR, "index pipeline completed", trace_id)
    ]
    assert result == view.to_dict()
    assert result["publicationStatus"] == "REVIEW_PENDING"
    assert [(event.trace_id, event.signal) for event in exporter.events] == [
        (trace_id, "indexing")
    ]


@pytest.mark.asyncio
async def test_cleanup_worker_returns_the_executor_outcome() -> None:
    document_id = uuid4()
    request_id = uuid4()
    outcome = CleanupOutcome(
        document_id=document_id,
        decision=CleanupDecision.COMPLETED,
        request_id=request_id,
    )
    executor = StubCleanupExecutor(outcome)

    result = await _run_cleanup(document_id, executor)  # type: ignore[arg-type]

    assert executor.calls == [document_id]
    assert result == outcome.to_dict()
