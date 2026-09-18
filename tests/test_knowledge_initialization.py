from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.models.operations import (
    CorpusReleaseRecord,
    KnowledgeInitializationAuditRecord,
    KnowledgeInitializationRunRecord,
)
from erp_assistant.operations.initialization_worker import (
    sweep_stale_initialization_runs,
)
from erp_assistant.operations.knowledge_initialization import (
    InitializationCommand,
    InitializationError,
    InitializationStatus,
    KnowledgeInitialization,
    execute_initialization_build,
    runtime_options,
)
from erp_assistant.orchestration.providers import embedding_binding_fingerprint
from erp_assistant.rag.index_manifest import build_manifest, capture_corpus_snapshot
from erp_assistant.rag.index_provisioning import (
    IndexProvisioningError,
    IndexSpecification,
    InMemoryIndexProvisioner,
)
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore

TRACE_ID = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_initialization_is_idempotent_and_waits_for_content(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
    )
    status = await service.status(DataClassification.ORDINARY)

    assert status.action == "INITIALIZE"
    assert status.run is None
    assert [option.key for option in status.runtime_options] == ["local-fake-ordinary"]

    command = InitializationCommand(
        classification=DataClassification.ORDINARY,
        idempotency_key="first-install",
        runtime_key="local-fake-ordinary",
        actor_id="system-admin",
        trace_id=TRACE_ID,
    )
    first = await service.start(command)
    repeated = await service.start(command)

    assert first.id == repeated.id
    assert first.status is InitializationStatus.WAITING
    assert first.stage == "WAITING_FOR_CONTENT"
    assert first.profile_id is not None
    assert first.revision == 3
    assert first.next_action == "UPLOAD_AND_APPROVE_CONTENT"
    profile = await session.get(RetrievalProfileRecord, first.profile_id)
    assert profile is not None
    assert profile.status == "STAGING"
    assert profile.physical_index_locator is not None
    assert str(first.profile_id).replace("-", "")[:12] in profile.physical_index_locator
    assert profile.index_schema_fingerprint
    assert profile.embedding_binding_fingerprint
    assert await session.scalar(select(func.count(CorpusReleaseRecord.id))) == 0
    assert await session.scalar(select(func.count(KnowledgeInitializationRunRecord.id))) == 1
    assert (
        await session.scalar(select(func.count(KnowledgeInitializationAuditRecord.id)))
        == 2
    )


class RecoveringProvisioner(InMemoryIndexProvisioner):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def provision(self, specification: object) -> object:
        if self.fail:
            raise IndexProvisioningError(
                "INDEX_IDENTITY_COLLISION",
                "物理索引身份冲突",
                retryable=True,
            )
        return super().provision(specification)  # type: ignore[arg-type]


class FatalProvisioner(InMemoryIndexProvisioner):
    def provision(self, specification: object) -> object:
        del specification
        raise IndexProvisioningError(
            "INDEX_IDENTITY_COLLISION",
            "物理索引身份冲突",
            retryable=False,
        )


@pytest.mark.asyncio
async def test_failed_provision_can_retry_without_duplicate_profile(
    session: AsyncSession,
) -> None:
    provisioner = RecoveringProvisioner()
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        provisioner,
    )
    command = InitializationCommand(
        classification=DataClassification.ORDINARY,
        idempotency_key="retryable-install",
        runtime_key="local-fake-ordinary",
        actor_id="system-admin",
        trace_id=TRACE_ID,
    )

    failed = await service.start(command)
    assert failed.status is InitializationStatus.FAILED
    assert failed.failure_code == "INDEX_IDENTITY_COLLISION"
    assert failed.retryable is True
    profile_id = failed.profile_id

    with pytest.raises(InitializationError) as competing_restart:
        await service.start(
            InitializationCommand(
                classification=DataClassification.ORDINARY,
                idempotency_key="must-retry-existing-run",
                runtime_key="local-fake-ordinary",
                actor_id="system-admin",
                trace_id=TRACE_ID,
            )
        )
    assert competing_restart.value.code == "INITIALIZATION_RETRY_REQUIRED"
    assert competing_restart.value.run_id == failed.id
    assert await session.scalar(select(func.count(RetrievalProfileRecord.id))) == 1

    provisioner.fail = False
    recovered = await service.retry(
        failed.id,
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=failed.revision,
    )

    assert recovered.status is InitializationStatus.WAITING
    assert recovered.profile_id == profile_id
    assert recovered.attempt == 2
    assert await session.scalar(select(func.count(RetrievalProfileRecord.id))) == 1


@pytest.mark.asyncio
async def test_retry_abandons_staging_when_runtime_is_no_longer_available(
    session: AsyncSession,
) -> None:
    settings = Settings(app_env="development")
    service = KnowledgeInitialization(session, settings, RecoveringProvisioner())
    failed = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="runtime-removed",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    assert failed.profile_id is not None
    settings.app_env = "production"

    abandoned = await service.retry(
        failed.id,
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=failed.revision,
    )

    assert abandoned.failure_code == "RUNTIME_DEPLOYMENT_UNAVAILABLE"
    assert abandoned.retryable is False
    profile = await session.get(RetrievalProfileRecord, failed.profile_id)
    assert profile is not None
    assert profile.status == "RETIRED"
    assert (await service.status(DataClassification.ORDINARY)).action == "INITIALIZE"


@pytest.mark.asyncio
async def test_nonretryable_fresh_failure_can_be_abandoned_for_a_new_profile(
    session: AsyncSession,
) -> None:
    settings = Settings(app_env="development")
    failed_service = KnowledgeInitialization(session, settings, FatalProvisioner())
    failed = await failed_service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="fatal-first",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )

    assert failed.status is InitializationStatus.FAILED
    assert failed.retryable is False
    assert (await failed_service.status(DataClassification.ORDINARY)).action == "INITIALIZE"

    recovered = await KnowledgeInitialization(
        session,
        settings,
        InMemoryIndexProvisioner(),
    ).start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="fresh-after-fatal",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )

    assert recovered.status is InitializationStatus.WAITING
    assert recovered.profile_id != failed.profile_id
    abandoned = await session.get(RetrievalProfileRecord, failed.profile_id)
    assert abandoned is not None
    assert abandoned.status == "RETIRED"


@pytest.mark.asyncio
async def test_competing_start_conflicts_and_cancelled_run_never_activates(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
    )
    first = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="owner",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )

    with pytest.raises(InitializationError) as raised:
        await service.start(
            InitializationCommand(
                classification=DataClassification.ORDINARY,
                idempotency_key="competitor",
                runtime_key="local-fake-ordinary",
                actor_id="system-admin",
                trace_id=TRACE_ID,
            )
        )
    assert raised.value.code == "INITIALIZATION_ALREADY_RUNNING"
    assert raised.value.run_id == first.id

    with pytest.raises(InitializationError) as stale_revision:
        await service.cancel(
            first.id,
            actor_id="system-admin",
            trace_id=TRACE_ID,
            expected_revision=first.revision - 1,
        )
    assert stale_revision.value.code == "INITIALIZATION_REVISION_CONFLICT"

    cancelled = await service.cancel(
        first.id,
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=first.revision,
    )
    assert cancelled.status is InitializationStatus.CANCELLED
    assert cancelled.next_action == "START_NEW_INITIALIZATION"
    assert await session.scalar(select(func.count(CorpusReleaseRecord.id))) == 0

    replacement = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="replacement",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    assert replacement.status is InitializationStatus.WAITING
    assert replacement.profile_id != first.profile_id


@pytest.mark.asyncio
async def test_fresh_start_rejects_an_active_profile_that_requires_migration(
    session: AsyncSession,
) -> None:
    session.add(_profile("ACTIVE"))
    await session.commit()
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
    )

    with pytest.raises(InitializationError) as raised:
        await service.start(
            InitializationCommand(
                classification=DataClassification.ORDINARY,
                idempotency_key="fresh-must-not-bypass-migration",
                runtime_key="local-fake-ordinary",
                actor_id="system-admin",
                trace_id=TRACE_ID,
            )
        )

    assert raised.value.code == "MIGRATION_REQUIRED"


@pytest.mark.asyncio
async def test_invalid_legacy_migration_offers_fresh_initialization(
    session: AsyncSession,
) -> None:
    profile = _profile("ACTIVE")
    profile_id = profile.id
    session.add(profile)
    await session.commit()
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
    )

    result = await service.migrate_active(
        DataClassification.ORDINARY,
        idempotency_key="legacy-migration",
        actor_id="system-admin",
        trace_id=TRACE_ID,
    )

    assert result.status is InitializationStatus.FAILED
    assert result.failure_code == "LEGACY_RUNTIME_BINDING_UNAVAILABLE"
    assert result.next_action == "START_FRESH_INITIALIZATION"
    assert (await service.status(DataClassification.ORDINARY)).action == "INITIALIZE"
    stored_profile = await session.get(RetrievalProfileRecord, profile_id)
    assert stored_profile is not None
    assert stored_profile.status == "ACTIVE"


@pytest.mark.asyncio
async def test_legacy_migration_rejects_missing_physical_index_identity(
    session: AsyncSession,
) -> None:
    settings = Settings(app_env="development")
    profile = _resolvable_legacy_profile(settings)
    document = _approved_document()
    session.add_all([profile, document])
    await session.commit()
    snapshot = await capture_corpus_snapshot(session, DataClassification.ORDINARY)
    manifest = build_manifest(profile, snapshot)
    profile.index_build_hash = manifest.build_hash
    profile.index_build_corpus_hash = manifest.corpus_hash
    profile.index_build_document_count = manifest.document_count
    profile.index_build_completed_at = datetime.now(UTC)
    await session.commit()

    result = await KnowledgeInitialization(
        session,
        settings,
        InMemoryIndexProvisioner(),
    ).migrate_active(
        DataClassification.ORDINARY,
        idempotency_key="legacy-without-physical-identity",
        actor_id="system-admin",
        trace_id=TRACE_ID,
    )

    assert result.status is InitializationStatus.FAILED
    assert result.failure_code == "INDEX_NOT_FOUND"
    assert result.next_action == "START_FRESH_INITIALIZATION"


@pytest.mark.asyncio
async def test_valid_legacy_profile_migrates_without_rebuilding(
    session: AsyncSession,
) -> None:
    settings = Settings(app_env="development")
    profile = _resolvable_legacy_profile(settings)
    document = _approved_document()
    session.add_all([profile, document])
    await session.commit()
    snapshot = await capture_corpus_snapshot(session, DataClassification.ORDINARY)
    manifest = build_manifest(profile, snapshot)
    profile.index_build_hash = manifest.build_hash
    profile.index_build_corpus_hash = manifest.corpus_hash
    profile.index_build_document_count = manifest.document_count
    profile.index_build_completed_at = datetime.now(UTC)
    await session.commit()
    provisioner = InMemoryIndexProvisioner()
    specification = _legacy_index_specification(profile, settings)
    provisioner.provision(specification)
    service = KnowledgeInitialization(
        session,
        settings,
        provisioner,
    )

    result = await service.migrate_active(
        DataClassification.ORDINARY,
        idempotency_key="valid-migration",
        actor_id="system-admin",
        trace_id=TRACE_ID,
    )

    assert result.status is InitializationStatus.SUCCEEDED
    assert result.stage == "RELEASE_ACTIVE"
    release = await session.get(CorpusReleaseRecord, result.candidate_release_id)
    assert release is not None
    assert release.status == "ACTIVE"
    assert release.index_build_hash == manifest.build_hash
    with pytest.raises(InitializationError) as duplicate_migration:
        await service.migrate_active(
            DataClassification.ORDINARY,
            idempotency_key="migration-after-active-release",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    assert duplicate_migration.value.code == "MIGRATION_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_approved_content_freezes_candidate_and_dispatches_background_build(
    session: AsyncSession,
) -> None:
    dispatched: list[tuple[UUID, UUID]] = []
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: (
            dispatched.append((run_id, trace_id)) or task_id
        ),
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="candidate-build",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()

    queued = await service.continue_to_candidate(
        run.id,
        release_version="first-candidate-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )

    assert queued.status is InitializationStatus.QUEUED
    assert queued.stage == "CANDIDATE_BUILD"
    assert queued.candidate_release_id is not None
    assert dispatched == [(run.id, TRACE_ID)]
    release = await session.get(CorpusReleaseRecord, queued.candidate_release_id)
    assert release is not None
    assert release.status == "BUILDING"
    assert release.document_count == 1


@pytest.mark.asyncio
async def test_background_build_hands_candidate_to_existing_evaluation_workflow(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="worker-handoff",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="worker-handoff-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    assert queued.candidate_release_id is not None
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    stored = await session.get(KnowledgeInitializationRunRecord, queued.id)
    assert stored is not None
    assert stored.lease_owner is not None

    async def complete_staging_build(
        profile_id: UUID,
        registry: object,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        release_id: UUID,
    ) -> object:
        del registry
        async with session_factory() as worker_session:
            profile = await worker_session.get(RetrievalProfileRecord, profile_id)
            release = await worker_session.get(CorpusReleaseRecord, release_id)
            assert profile is not None
            assert release is not None
            profile.index_build_hash = release.index_build_hash
            profile.index_build_corpus_hash = release.corpus_hash
            profile.index_build_document_count = release.document_count
            profile.index_build_completed_at = datetime.now(UTC)
            await worker_session.commit()
        return object()

    monkeypatch.setattr(
        "erp_assistant.rag.staging_index.build_staging_index",
        complete_staging_build,
    )

    result = await execute_initialization_build(
        queued.id,
        factory,
        Settings(app_env="development"),
        lease_token=stored.lease_owner,
    )

    assert result.status is InitializationStatus.SUCCEEDED
    assert result.stage == "EVALUATION_HANDOFF"
    release = await session.get(CorpusReleaseRecord, queued.candidate_release_id)
    assert release is not None
    await session.refresh(release)
    assert release.status == "EVALUATING"
    assert (await service.status(DataClassification.ORDINARY)).action == "MANAGE_RELEASES"


@pytest.mark.asyncio
async def test_stale_worker_cannot_commit_after_a_new_attempt_takes_the_lease(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="fenced-worker",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="fenced-worker-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    queued_record = await session.get(KnowledgeInitializationRunRecord, queued.id)
    assert queued_record is not None
    assert queued_record.lease_owner is not None
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def paused_build(*args: object, **kwargs: object) -> object:
        del args, kwargs
        started.set()
        await proceed.wait()
        return object()

    monkeypatch.setattr(
        "erp_assistant.rag.staging_index.build_staging_index",
        paused_build,
    )
    with pytest.raises(InitializationError) as stale_delivery:
        await execute_initialization_build(
            queued.id,
            factory,
            Settings(app_env="development"),
            lease_token="task:stale",
        )
    assert stale_delivery.value.code == "INITIALIZATION_BUILD_LEASE_REJECTED"
    old_worker = asyncio.create_task(
        execute_initialization_build(
            queued.id,
            factory,
            Settings(app_env="development"),
            lease_token=queued_record.lease_owner,
        )
    )
    await started.wait()
    async with factory() as takeover_session:
        stored = await takeover_session.get(KnowledgeInitializationRunRecord, queued.id)
        assert stored is not None
        stored.lease_owner = "task:new"
        stored.attempt += 1
        stored.revision += 1
        await takeover_session.commit()
    proceed.set()

    observed = await old_worker

    assert observed.status is InitializationStatus.RUNNING
    async with factory() as verification_session:
        stored = await verification_session.get(KnowledgeInitializationRunRecord, queued.id)
        release = await verification_session.get(
            CorpusReleaseRecord, queued.candidate_release_id
        )
        assert stored is not None
        assert stored.lease_owner == "task:new"
        assert release is not None
        assert release.status == "BUILDING"


@pytest.mark.asyncio
async def test_expired_candidate_worker_lease_becomes_retryable_failure(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="expired-worker",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="expired-worker-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    stored = await session.get(KnowledgeInitializationRunRecord, queued.id)
    assert stored is not None
    stored.status = InitializationStatus.RUNNING.value
    stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    observed = await service.status(DataClassification.ORDINARY)

    assert observed.run is not None
    assert observed.run.status is InitializationStatus.FAILED
    assert observed.run.failure_code == "INITIALIZATION_WORKER_LEASE_EXPIRED"
    assert observed.run.retryable is True


@pytest.mark.asyncio
async def test_expired_queued_dispatch_becomes_retryable_failure(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development", knowledge_index_lease_seconds=30),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="expired-dispatch",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="expired-dispatch-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    stored = await session.get(KnowledgeInitializationRunRecord, queued.id)
    assert stored is not None
    assert stored.lease_expires_at is not None
    stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    observed = await service.status(DataClassification.ORDINARY)

    assert observed.run is not None
    assert observed.run.status is InitializationStatus.FAILED
    assert observed.run.failure_code == "INITIALIZATION_DISPATCH_EXPIRED"
    assert observed.run.retryable is True


@pytest.mark.asyncio
async def test_periodic_sweep_expires_stale_initialization_without_a_status_read(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="sweep-expired",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="sweep-expired-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    stored = await session.get(KnowledgeInitializationRunRecord, queued.id)
    assert stored is not None
    stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    count = await sweep_stale_initialization_runs(factory)

    assert count == 1
    await session.refresh(stored)
    assert stored.status == InitializationStatus.FAILED.value
    assert stored.failure_code == "INITIALIZATION_DISPATCH_EXPIRED"


@pytest.mark.asyncio
async def test_cancelling_candidate_marks_release_failed(
    session: AsyncSession,
) -> None:
    service = KnowledgeInitialization(
        session,
        Settings(app_env="development"),
        InMemoryIndexProvisioner(),
        lambda run_id, trace_id, task_id: task_id,
    )
    run = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="cancel-candidate",
            runtime_key="local-fake-ordinary",
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )
    session.add(_approved_document())
    await session.commit()
    queued = await service.continue_to_candidate(
        run.id,
        release_version="cancel-candidate-v1",
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=run.revision,
    )
    release = await session.get(CorpusReleaseRecord, queued.candidate_release_id)
    assert release is not None
    release.status = "READY"
    await session.commit()

    cancelled = await service.cancel(
        queued.id,
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=queued.revision,
    )

    assert cancelled.status is InitializationStatus.CANCELLED
    await session.refresh(release)
    assert release.status == "FAILED"
    assert release.failure_reason_code == "INITIALIZATION_CANCELLED"


def _profile(status: str) -> RetrievalProfileRecord:
    return RetrievalProfileRecord(
        id=uuid4(),
        profile_key=f"legacy-{uuid4()}",
        version=1,
        data_classification="ORDINARY",
        provider_boundary="INTERNAL",
        embedding_deployment="local-fake",
        embedding_model="fake-lexical-v1",
        embedding_dimension=256,
        rerank_deployment="local-fake",
        rerank_model="fake-lexical-rerank-v2",
        index_identity=f"legacy-index-{uuid4()}",
        parser_version="pypdf-v1",
        chunk_version="structured-window-v1",
        retrieval_config={"retrieval": "dense-bm25-rrf-v1"},
        configuration_fingerprint="c" * 64,
        production_ready=False,
        status=status,
    )


def _resolvable_legacy_profile(settings: Settings) -> RetrievalProfileRecord:
    binding = next(
        item
        for item in build_retrieval_runtime_registry(settings).bindings
        if item.data_classification is DataClassification.ORDINARY
    )
    return RetrievalProfileRecord(
        id=uuid4(),
        profile_key=f"legacy-{uuid4()}",
        version=1,
        data_classification="ORDINARY",
        provider_boundary=binding.provider_boundary.value,
        embedding_deployment=binding.embedding.deployment,
        embedding_model=binding.embedding.model,
        embedding_dimension=binding.embedding.dimension,
        rerank_deployment=binding.reranker.deployment,
        rerank_model=binding.reranker.model,
        index_identity=binding.vector_store.identity,
        parser_version=binding.parser_version,
        chunk_version=binding.chunk_version,
        retrieval_config=binding.retrieval_config,
        configuration_fingerprint=binding.configuration_fingerprint,
        production_ready=binding.production_ready,
        status="ACTIVE",
    )


def _legacy_index_specification(
    profile: RetrievalProfileRecord, settings: Settings
) -> IndexSpecification:
    runtime = build_retrieval_runtime_registry(settings).binding_for(profile)
    store = runtime.vector_store
    assert isinstance(store, MilvusVectorStore)
    return IndexSpecification.create(
        uri=store.uri,
        physical_locator=store.collection,
        dimension=profile.embedding_dimension,
        embedding_binding_fingerprint=embedding_binding_fingerprint(runtime.embedding),
        index_identity=store.identity,
    )


def _approved_document() -> KnowledgeDocument:
    document_id = uuid4()
    return KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key=f"first-{document_id}",
        version=1,
        title="首批知识",
        original_filename="first.md",
        media_type="text/markdown",
        data_classification="ORDINARY",
        size_bytes=100,
        sha256="a" * 64,
        object_key=f"knowledge/{document_id}",
        status="READY",
        active=True,
        publication_status="PUBLISHED",
        retention_status="RETAINED",
        governance_revision=1,
        index_artifacts_present=True,
        created_by="system-admin",
    )


def test_production_runtime_choices_exclude_fake_defaults_and_secrets() -> None:
    settings = _production_settings()

    options = runtime_options(settings, DataClassification.ORDINARY)

    assert len(options) == 1
    assert options[0].production_ready is True
    payload = str(options[0].to_dict())
    assert "local-fake" not in payload
    assert "embedding-secret" not in payload
    assert "rerank-secret" not in payload


def test_runtime_key_changes_when_immutable_runtime_configuration_changes() -> None:
    first = _production_settings()
    changed = _production_settings()
    deployment = changed.retrieval_runtime_deployments[0]
    changed.retrieval_runtime_deployments[0] = deployment.model_copy(
        update={
            "vector_index": deployment.vector_index.model_copy(
                update={"uri": "http://other-milvus.internal:19530"}
            ),
            "candidate_multiplier": deployment.candidate_multiplier + 1,
        }
    )

    first_key = runtime_options(first, DataClassification.ORDINARY)[0].key
    changed_key = runtime_options(changed, DataClassification.ORDINARY)[0].key

    assert changed_key != first_key


@pytest.mark.asyncio
async def test_production_preflight_failure_is_safe_and_retryable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_preflight(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("secret backend detail")

    monkeypatch.setattr(
        "erp_assistant.operations.production_probes.validate_production_runtime_startup",
        fail_preflight,
    )
    settings = _production_settings()
    option = runtime_options(settings, DataClassification.ORDINARY)[0]
    service = KnowledgeInitialization(
        session,
        settings,
        InMemoryIndexProvisioner(),
    )

    result = await service.start(
        InitializationCommand(
            classification=DataClassification.ORDINARY,
            idempotency_key="production-preflight",
            runtime_key=option.key,
            actor_id="system-admin",
            trace_id=TRACE_ID,
        )
    )

    assert result.status is InitializationStatus.FAILED
    assert result.failure_code == "INITIALIZATION_PREFLIGHT_FAILED"
    assert result.failure_message == "生产依赖预检未通过，请检查依赖状态后重试"
    assert "secret" not in result.failure_message
    assert result.retryable is True
    assert await session.scalar(select(func.count(RetrievalProfileRecord.id))) == 0

    async def pass_preflight(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "erp_assistant.operations.production_probes.validate_production_runtime_startup",
        pass_preflight,
    )
    recovered = await service.retry(
        result.id,
        actor_id="system-admin",
        trace_id=TRACE_ID,
        expected_revision=result.revision,
    )

    assert recovered.status is InitializationStatus.WAITING
    assert recovered.attempt == 2


def _production_settings() -> Settings:
    return Settings(
        app_env="production",
        retrieval_runtime_deployments=[
            {
                "data_classification": "ORDINARY",
                "provider_boundary": "CLOUD",
                "parser_version": "pypdf-v1",
                "chunk_version": "structured-window-v1",
                "embedding": {
                    "base_url": "https://models.example/v1",
                    "api_key": "embedding-secret",
                    "deployment": "embed-prod",
                    "model": "semantic-embed",
                    "version": "v1",
                    "dimension": 1536,
                },
                "reranker": {
                    "base_url": "https://models.example/v1",
                    "api_key": "rerank-secret",
                    "deployment": "rerank-prod",
                    "model": "semantic-rerank",
                    "version": "v1",
                },
                "vector_index": {
                    "uri": "http://milvus.internal:19530",
                    "collection": "ordinary-production",
                },
            }
        ],
    )
