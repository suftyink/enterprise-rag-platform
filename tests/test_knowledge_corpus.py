from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
)
from erp_assistant.rag.document_indexer import (
    DocumentIndexOutcome,
    IndexDecision,
    IndexPipelineIdentity,
)
from erp_assistant.rag.knowledge_corpus import (
    CorpusActor,
    CorpusError,
    KnowledgeCorpus,
    LifecycleAction,
    PublicationStatus,
)
from erp_assistant.rag.types import (
    CleanupKind,
    CleanupStatus,
    DocumentStatus,
    IndexJobStatus,
    RetentionStatus,
)


@dataclass
class ControlledProcessor:
    outcome: DocumentIndexOutcome

    async def index(self, document_id: UUID) -> DocumentIndexOutcome:
        assert document_id == self.outcome.document_id
        return self.outcome


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
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_submission_waits_for_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document(version=1)
    async with session_factory() as session:
        session.add_all(
            [
                document,
                KnowledgeIndexJob(
                    document_id=document.id,
                    status=IndexJobStatus.QUEUED.value,
                    phase=DocumentStatus.QUEUED.value,
                ),
            ]
        )
        await session.commit()

    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(document.id)),
    )
    view = await corpus.submit(document.id, _actor(), reason="initial ingestion")

    assert view.processing_status is DocumentStatus.READY
    assert view.publication_status is PublicationStatus.REVIEW_PENDING
    assert view.retrieval_eligible is False
    assert view.revision == 0
    assert view.audit_events[-1].action == "SUBMIT"
    assert view.audit_events[-1].actor_id == "reviewer-a"
    assert view.audit_events[-1].created_at is not None


@pytest.mark.asyncio
async def test_publish_switches_versions_atomically_and_withdraws_immediately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _document(version=1)
    second = _document(version=2)
    async with session_factory() as session:
        session.add_all([first, second])
        await session.commit()

    first_corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(first.id)),
    )
    await first_corpus.submit(first.id, _actor(), reason="index v1")
    published_first = await first_corpus.transition(
        first.id,
        LifecycleAction.PUBLISH,
        _actor(),
        reason="approved v1",
        expected_revision=0,
    )
    assert published_first.publication_status is PublicationStatus.PUBLISHED
    assert published_first.retrieval_eligible is True
    assert published_first.revision == 1

    second_corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(second.id)),
    )
    await second_corpus.submit(second.id, _actor(), reason="index v2")
    published_second = await second_corpus.transition(
        second.id,
        LifecycleAction.PUBLISH,
        _actor(),
        reason="approved v2",
        expected_revision=1,
    )
    old_view = await second_corpus.inspect(first.id, _actor())
    assert published_second.publication_status is PublicationStatus.PUBLISHED
    assert published_second.retrieval_eligible is True
    assert old_view.publication_status is PublicationStatus.SUPERSEDED
    assert old_view.retrieval_eligible is False

    withdrawn = await second_corpus.transition(
        second.id,
        LifecycleAction.WITHDRAW,
        _actor(),
        reason="unsafe content",
        expected_revision=2,
    )
    assert withdrawn.publication_status is PublicationStatus.WITHDRAWN
    assert withdrawn.retrieval_eligible is False
    assert withdrawn.cleanup_requests[-1].kind is CleanupKind.VECTOR
    assert withdrawn.cleanup_requests[-1].idempotency_key
    assert withdrawn.audit_events[-1].reason == "unsafe content"


@pytest.mark.asyncio
async def test_transition_rejects_stale_revision_without_changing_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document(version=1)
    async with session_factory() as session:
        session.add(document)
        await session.commit()
    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(document.id)),
    )
    await corpus.submit(document.id, _actor(), reason="index")
    await corpus.transition(
        document.id,
        LifecycleAction.REJECT,
        _actor(),
        reason="incorrect scope",
        expected_revision=0,
    )

    with pytest.raises(CorpusError, match="修订") as raised:
        await corpus.transition(
            document.id,
            LifecycleAction.RESUBMIT,
            _actor(),
            reason="fixed metadata",
            expected_revision=0,
        )

    assert raised.value.code == "STALE_CORPUS_REVISION"
    view = await corpus.inspect(document.id, _actor())
    assert view.publication_status is PublicationStatus.REJECTED
    assert view.revision == 1
    assert view.audit_events[-1].action == "RESUBMIT"
    assert view.audit_events[-1].result == "STALE_CORPUS_REVISION"


@pytest.mark.asyncio
async def test_resubmit_rollback_purge_and_cleanup_compensation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = _document(version=1)
    second = _document(version=2)
    async with session_factory() as session:
        session.add_all([first, second])
        await session.commit()
    first_corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(first.id)),
    )
    second_corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(second.id)),
    )
    await first_corpus.submit(first.id, _actor(), reason="index v1")
    await first_corpus.transition(
        first.id,
        LifecycleAction.PUBLISH,
        _actor(),
        reason="publish v1",
        expected_revision=0,
    )
    await second_corpus.submit(second.id, _actor(), reason="index v2")
    await second_corpus.transition(
        second.id,
        LifecycleAction.PUBLISH,
        _actor(),
        reason="publish v2",
        expected_revision=1,
    )
    await second_corpus.transition(
        second.id,
        LifecycleAction.WITHDRAW,
        _actor(),
        reason="withdraw v2",
        expected_revision=2,
    )
    await second_corpus.transition(
        second.id,
        LifecycleAction.RESUBMIT,
        _actor(),
        reason="review again",
        expected_revision=3,
    )
    republished = await second_corpus.transition(
        second.id,
        LifecycleAction.PUBLISH,
        _actor(),
        reason="republish v2",
        expected_revision=4,
    )
    assert republished.retrieval_eligible is True
    assert republished.cleanup_requests[-1].status is CleanupStatus.CANCELLED

    rolled_back = await first_corpus.transition(
        first.id,
        LifecycleAction.ROLLBACK,
        _actor(),
        reason="restore known good version",
        expected_revision=5,
    )
    assert rolled_back.publication_status is PublicationStatus.PUBLISHED
    assert rolled_back.retrieval_eligible is True
    assert (await second_corpus.inspect(second.id, _actor())).publication_status is (
        PublicationStatus.SUPERSEDED
    )

    purge_requested = await second_corpus.transition(
        second.id,
        LifecycleAction.REQUEST_PURGE,
        _actor(),
        reason="retention expired",
        expected_revision=6,
    )
    assert purge_requested.retention_status is RetentionStatus.PURGE_REQUESTED
    assert purge_requested.cleanup_requests[-1].kind is CleanupKind.VECTOR_AND_OBJECT
    with pytest.raises(CorpusError) as repeated_purge:
        await second_corpus.transition(
            second.id,
            LifecycleAction.REQUEST_PURGE,
            _actor(),
            reason="duplicate purge request",
            expected_revision=7,
        )
    assert repeated_purge.value.code == "INVALID_CORPUS_TRANSITION"
    after_repeated_purge = await second_corpus.inspect(second.id, _actor())
    assert after_repeated_purge.revision == 7
    assert len(after_repeated_purge.cleanup_requests) == 2
    failed = await second_corpus.transition(
        second.id,
        LifecycleAction.MARK_CLEANUP_FAILED,
        _actor(),
        reason="object store unavailable",
        expected_revision=7,
    )
    assert failed.cleanup_requests[-1].status is CleanupStatus.FAILED
    assert failed.cleanup_requests[-1].attempt == 1
    retried = await second_corpus.transition(
        second.id,
        LifecycleAction.RETRY_CLEANUP,
        _actor(),
        reason="retry after recovery",
        expected_revision=8,
    )
    assert retried.cleanup_requests[-1].status is CleanupStatus.PENDING
    purged = await second_corpus.transition(
        second.id,
        LifecycleAction.COMPLETE_CLEANUP,
        _actor(),
        reason="all artifacts removed",
        expected_revision=9,
    )
    assert purged.retention_status is RetentionStatus.PURGED
    assert purged.retrieval_eligible is False
    assert purged.cleanup_requests[-1].status is CleanupStatus.COMPLETED


@pytest.mark.asyncio
async def test_inspect_exposes_source_warnings_and_chunk_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document(version=1)
    chunks = [
        _chunk(document, index=0, page_start=1, page_end=1, content="first chunk"),
        _chunk(document, index=1, page_start=2, page_end=3, content="second chunk"),
    ]
    async with session_factory() as session:
        session.add_all([document, *chunks])
        await session.commit()
    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(document.id)),
    )

    view = await corpus.submit(document.id, _actor(), reason="inspectable ingestion")

    assert view.original_filename == "inventory-v1.pdf"
    assert view.warnings == ("LOW_TEXT_PAGE",)
    assert view.pipeline_revision == "pipeline-v1"
    assert view.chunk_summary.chunk_count == 2
    assert view.chunk_summary.character_count == len("first chunksecond chunk")
    assert (view.chunk_summary.first_page, view.chunk_summary.last_page) == (1, 3)


@pytest.mark.asyncio
async def test_untrusted_actor_is_rejected_at_the_corpus_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document(version=1)
    async with session_factory() as session:
        session.add(document)
        await session.commit()
    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(document.id)),
    )

    with pytest.raises(CorpusError) as raised:
        await corpus.inspect(
            document.id,
            CorpusActor(subject_id="employee-a", tenant_id="tenant-a"),
        )

    assert raised.value.code == "CORPUS_FORBIDDEN"


@pytest.mark.asyncio
async def test_submit_requires_a_new_version_for_published_content(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document = _document(version=1)
    document.status = DocumentStatus.READY.value
    document.active = True
    document.publication_status = PublicationStatus.PUBLISHED.value
    document.index_artifacts_present = True
    async with session_factory() as session:
        session.add(document)
        await session.commit()
    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=ControlledProcessor(_outcome(document.id)),
    )

    with pytest.raises(CorpusError) as raised:
        await corpus.submit(document.id, _actor(), reason="unsafe in-place rebuild")

    assert raised.value.code == "PUBLISHED_DOCUMENT_REQUIRES_NEW_VERSION"


def _document(*, version: int, document_key: str = "inventory-sop") -> KnowledgeDocument:
    document_id = uuid4()
    return KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key=document_key,
        version=version,
        title=f"Inventory SOP v{version}",
        original_filename=f"inventory-v{version}.pdf",
        media_type="application/pdf",
        data_classification="ORDINARY",
        size_bytes=100,
        sha256=uuid4().hex + uuid4().hex,
        object_key=f"knowledge/{document_id}/original.pdf",
        status=DocumentStatus.QUEUED.value,
        active=False,
        created_by="author-a",
    )


def _outcome(document_id: UUID) -> DocumentIndexOutcome:
    return DocumentIndexOutcome(
        document_id=document_id,
        decision=IndexDecision.INDEXED_FOR_CURRENT_POLICY,
        status=DocumentStatus.READY,
        chunk_count=2,
        warnings=("LOW_TEXT_PAGE",),
        attempt=1,
        pipeline=IndexPipelineIdentity(
            parser="parser-v1",
            chunking="chunk-v1",
            embedding="embed-v1",
            vector_index="vector-v1",
            revision="pipeline-v1",
        ),
    )


def _actor() -> CorpusActor:
    return CorpusActor(subject_id="reviewer-a", tenant_id="tenant-a", trusted=True)


def _chunk(
    document: KnowledgeDocument,
    *,
    index: int,
    page_start: int,
    page_end: int,
    content: str,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        document_id=document.id,
        scope=document.scope,
        scope_id=document.scope_id,
        version=document.version,
        chunk_index=index,
        page_start=page_start,
        page_end=page_end,
        content=content,
        content_hash=uuid4().hex + uuid4().hex,
        char_count=len(content),
        embedding_model="embedding-v1",
    )
