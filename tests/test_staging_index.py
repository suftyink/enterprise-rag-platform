from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.models.operations import (
    CorpusReleaseDocumentRecord,
    CorpusReleaseRecord,
)
from erp_assistant.operations.release_contracts import ReleaseStatus
from erp_assistant.operations.release_store import CorpusReleaseStore
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.index_manifest import CorpusSnapshot
from erp_assistant.rag.reindex import ReindexResult
from erp_assistant.rag.staging_index import build_staging_index


@pytest.mark.asyncio
async def test_candidate_snapshot_drift_marks_release_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    document_id = uuid4()
    profile = RetrievalProfileRecord(
        profile_key="candidate",
        version=1,
        data_classification="ORDINARY",
        provider_boundary="INTERNAL",
        embedding_deployment="embedding-v1",
        embedding_model="embedding-model",
        embedding_dimension=3,
        rerank_deployment="rerank-v1",
        rerank_model="rerank-model",
        index_identity="milvus-v3:candidate",
        parser_version="parser-v1",
        chunk_version="chunk-v1",
        retrieval_config={},
        configuration_fingerprint="c" * 64,
        production_ready=True,
        status="STAGING",
    )
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="candidate-doc",
        version=1,
        title="候选文档",
        original_filename="candidate.md",
        media_type="text/markdown",
        data_classification="ORDINARY",
        size_bytes=10,
        sha256="d" * 64,
        object_key="knowledge/candidate.md",
        status="READY",
        active=True,
        publication_status="PUBLISHED",
        retention_status="RETAINED",
        governance_revision=1,
        index_artifacts_present=True,
        created_by="system-admin",
    )
    async with factory() as session:
        session.add_all([profile, document])
        await session.flush()
        release = CorpusReleaseStore.new_release(
            release_version="candidate-v1",
            profile=profile,
            corpus_generation=1,
            corpus_hash="a" * 64,
            document_count=1,
            created_by="system-admin",
            status=ReleaseStatus.BUILDING,
        )
        session.add(release)
        session.add(
            CorpusReleaseDocumentRecord(
                release_id=release.id,
                document_id=document_id,
                document_sha256=document.sha256,
                governance_revision=document.governance_revision,
                created_at=release.created_at,
            )
        )
        await session.commit()
        release_id = release.id
        profile_id = profile.id

    snapshots = iter(
        [
            CorpusSnapshot((document_id,), "a" * 64),
            CorpusSnapshot((document_id,), "b" * 64),
        ]
    )

    async def snapshot(*args: object, **kwargs: object) -> CorpusSnapshot:
        del args, kwargs
        return next(snapshots)

    async def reindex(*args: object, **kwargs: object) -> ReindexResult:
        del args, kwargs
        return ReindexResult(1, 1, 0, 0, 0, 0, 1)

    class FakeIndexer:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.pipeline = SimpleNamespace(
                parser="parser-v1",
                chunking="chunk-v1",
                vector_index="milvus-v3:candidate",
            )

    binding = SimpleNamespace(embedding=object(), vector_store=object(), settings=object())
    registry = SimpleNamespace(binding_for=lambda candidate: binding)
    monkeypatch.setattr("erp_assistant.rag.staging_index.DocumentIndexer", FakeIndexer)
    monkeypatch.setattr("erp_assistant.rag.staging_index.get_object_storage", lambda: object())
    monkeypatch.setattr("erp_assistant.rag.staging_index.PypdfParser", lambda: object())
    monkeypatch.setattr("erp_assistant.rag.staging_index.capture_document_snapshot", snapshot)
    monkeypatch.setattr("erp_assistant.rag.staging_index.reindex_active_documents", reindex)

    with pytest.raises(RagError) as caught:
        await build_staging_index(
            profile_id,
            registry,  # type: ignore[arg-type]
            factory,
            release_id=release_id,
        )
    assert caught.value.code == "CANDIDATE_CORPUS_DRIFT"
    async with factory() as session:
        failed = await session.get_one(CorpusReleaseRecord, release_id)
        assert failed.status == ReleaseStatus.FAILED.value
        assert failed.failure_reason_code == "CANDIDATE_CORPUS_DRIFT"
    await engine.dispose()
