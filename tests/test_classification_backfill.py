from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import (
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeLifecycleEvent,
)
from erp_assistant.rag.classification_backfill import backfill_document_classifications
from erp_assistant.rag.errors import RagDependencyError, RagError
from erp_assistant.rag.types import DataClassification


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _document(digest: str, classification: DataClassification) -> KnowledgeDocument:
    document_id = uuid4()
    return KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key=f"document-{digest[0]}",
        version=1,
        title="Document",
        original_filename="document.md",
        media_type="text/markdown",
        data_classification=classification.value,
        size_bytes=100,
        sha256=digest,
        object_key=f"knowledge/{document_id}/original.md",
        status="READY",
        active=True,
        publication_status="PUBLISHED",
        retention_status="RETAINED",
        index_artifacts_present=True,
        created_by="legacy-import",
    )


@pytest.mark.asyncio
async def test_backfill_classifies_audits_and_dispatches_reindex_idempotently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pending = _document("a" * 64, DataClassification.UNCLASSIFIED)
    existing = _document("b" * 64, DataClassification.RESTRICTED)
    async with session_factory() as session:
        session.add_all([pending, existing])
        await session.commit()
    dispatched: list[UUID] = []

    def dispatch(document_id: UUID, trace_id: UUID) -> str:
        del trace_id
        dispatched.append(document_id)
        return f"task-{document_id}"

    manifest = {
        "a" * 64: DataClassification.ORDINARY,
        "b" * 64: DataClassification.RESTRICTED,
    }
    result = await backfill_document_classifications(
        session_factory,
        dispatch,
        manifest=manifest,
        operator_id="security-reviewer",
    )
    repeated = await backfill_document_classifications(
        session_factory,
        dispatch,
        manifest=manifest,
        operator_id="security-reviewer",
    )

    assert result.classified == 1
    assert result.already_classified == 1
    assert result.dispatched == 1
    assert repeated.classified == 0
    assert repeated.already_classified == 2
    assert dispatched == [pending.id]
    async with session_factory() as session:
        stored = await session.get(KnowledgeDocument, pending.id)
        jobs = (await session.scalars(select(KnowledgeIndexJob))).all()
        events = (await session.scalars(select(KnowledgeLifecycleEvent))).all()
    assert stored is not None
    assert stored.data_classification == DataClassification.ORDINARY.value
    assert len(jobs) == 1
    assert jobs[0].celery_task_id == f"task-{pending.id}"
    assert [(event.action, event.actor_id) for event in events] == [
        ("CLASSIFY", "security-reviewer")
    ]


@pytest.mark.asyncio
async def test_backfill_rejects_manifest_that_omits_unclassified_documents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(_document("c" * 64, DataClassification.UNCLASSIFIED))
        await session.commit()

    with pytest.raises(RagError) as raised:
        await backfill_document_classifications(
            session_factory,
            lambda document_id, trace_id: str(document_id),
            manifest={"d" * 64: DataClassification.ORDINARY},
            operator_id="security-reviewer",
        )

    assert raised.value.code == "CLASSIFICATION_MANIFEST_INCOMPLETE"


@pytest.mark.asyncio
async def test_backfill_continues_dispatching_and_retries_only_failed_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _document("e" * 64, DataClassification.UNCLASSIFIED)
    second = _document("f" * 64, DataClassification.UNCLASSIFIED)
    second.document_key = "document-f"
    async with session_factory() as session:
        session.add_all([first, second])
        await session.commit()
    calls: list[UUID] = []

    def flaky_dispatch(document_id: UUID, trace_id: UUID) -> str:
        del trace_id
        calls.append(document_id)
        if document_id == first.id:
            raise RuntimeError("queue unavailable")
        return f"task-{document_id}"

    manifest = {
        "e" * 64: DataClassification.ORDINARY,
        "f" * 64: DataClassification.RESTRICTED,
    }
    with pytest.raises(RagDependencyError) as raised:
        await backfill_document_classifications(
            session_factory,
            flaky_dispatch,
            manifest=manifest,
            operator_id="security-reviewer",
        )
    assert raised.value.code == "CLASSIFICATION_REINDEX_DISPATCH_FAILED"
    assert calls == [first.id, second.id]

    retried: list[UUID] = []

    def retry_dispatch(document_id: UUID, trace_id: UUID) -> str:
        del trace_id
        retried.append(document_id)
        return f"retry-{document_id}"

    result = await backfill_document_classifications(
        session_factory,
        retry_dispatch,
        manifest=manifest,
        operator_id="security-reviewer",
    )

    assert result.classified == 0
    assert result.already_classified == 2
    assert result.dispatched == 1
    assert retried == [first.id]


@pytest.mark.asyncio
async def test_backfill_recovers_committed_outbox_after_process_exit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document("1" * 64, DataClassification.ORDINARY)
    job = KnowledgeIndexJob(
        id=uuid4(),
        document_id=document.id,
        status="QUEUED",
        phase="QUEUED",
    )
    event = KnowledgeLifecycleEvent(
        document_id=document.id,
        action="CLASSIFY",
        actor_id="security-reviewer",
        reason="trusted SHA-256 manifest: ORDINARY",
        document_version=1,
        revision=1,
        result="APPLIED",
    )
    async with session_factory() as session:
        session.add_all([document, job, event])
        await session.commit()
    dispatched: list[UUID] = []

    def dispatch(document_id: UUID, trace_id: UUID) -> str:
        del trace_id
        dispatched.append(document_id)
        return f"task-{document_id}"

    result = await backfill_document_classifications(
        session_factory,
        dispatch,
        manifest={"1" * 64: DataClassification.ORDINARY},
        operator_id="security-reviewer",
    )

    assert result.classified == 0
    assert result.dispatched == 1
    assert dispatched == [document.id]
