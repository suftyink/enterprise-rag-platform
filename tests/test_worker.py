from uuid import uuid4

import pytest

from erp_assistant.rag.cleanup_executor import CleanupDecision, CleanupOutcome
from erp_assistant.rag.knowledge_corpus import (
    SYSTEM_CORPUS_ACTOR,
    CorpusChunkSummary,
    CorpusDocumentView,
)
from erp_assistant.rag.types import DocumentStatus, PublicationStatus, RetentionStatus
from erp_assistant.workers.celery_app import celery_app
from erp_assistant.workers.tasks import (
    _run_cleanup,
    _run_index,
    cleanup_knowledge_document,
    health,
    index_document,
)


class StubCorpus:
    def __init__(self, view: CorpusDocumentView) -> None:
        self.view = view
        self.calls: list[tuple[object, object, str]] = []

    async def submit(
        self, document_id: object, actor: object, *, reason: str
    ) -> CorpusDocumentView:
        self.calls.append((document_id, actor, reason))
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
    assert health.run() == {"status": "ok"}


def test_index_worker_acknowledges_only_after_persisted_outcome() -> None:
    assert index_document.acks_late is True
    assert index_document.reject_on_worker_lost is True
    assert cleanup_knowledge_document.acks_late is True
    assert cleanup_knowledge_document.reject_on_worker_lost is True


@pytest.mark.asyncio
async def test_index_worker_returns_the_corpus_outcome() -> None:
    document_id = uuid4()
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

    result = await _run_index(document_id, corpus)  # type: ignore[arg-type]

    assert corpus.calls == [(document_id, SYSTEM_CORPUS_ACTOR, "index pipeline completed")]
    assert result == view.to_dict()
    assert result["publicationStatus"] == "REVIEW_PENDING"


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
