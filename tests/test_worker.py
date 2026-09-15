from uuid import uuid4

import pytest

from erp_assistant.rag.document_indexer import (
    DocumentIndexOutcome,
    IndexDecision,
    IndexPipelineIdentity,
)
from erp_assistant.rag.types import DocumentStatus
from erp_assistant.workers.celery_app import celery_app
from erp_assistant.workers.tasks import _run_index, health, index_document


class StubIndexer:
    def __init__(self, outcome: DocumentIndexOutcome) -> None:
        self.outcome = outcome
        self.calls: list[object] = []

    async def index(self, document_id: object) -> DocumentIndexOutcome:
        self.calls.append(document_id)
        return self.outcome


def test_health_task_is_registered_and_side_effect_free() -> None:
    celery_app.finalize()
    assert "erp_assistant.health" in celery_app.tasks
    assert health.run() == {"status": "ok"}


def test_index_worker_acknowledges_only_after_persisted_outcome() -> None:
    assert index_document.acks_late is True
    assert index_document.reject_on_worker_lost is True


@pytest.mark.asyncio
async def test_index_worker_returns_the_indexer_outcome() -> None:
    document_id = uuid4()
    pipeline = IndexPipelineIdentity(
        parser="parser-v1",
        chunking="chunk-v1",
        embedding="embedding-v1",
        vector_index="index-v1",
        revision="a" * 64,
    )
    outcome = DocumentIndexOutcome(
        document_id=document_id,
        decision=IndexDecision.OCR_REQUIRED,
        status=DocumentStatus.OCR_REQUIRED,
        chunk_count=0,
        warnings=("PAGE_1_NO_TEXT",),
        attempt=1,
        pipeline=pipeline,
        error_code="OCR_REQUIRED",
    )
    indexer = StubIndexer(outcome)

    result = await _run_index(document_id, indexer)  # type: ignore[arg-type]

    assert indexer.calls == [document_id]
    assert result == outcome.to_dict()
    assert result["decision"] == "OCR_REQUIRED"
