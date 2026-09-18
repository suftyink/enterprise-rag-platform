from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.models.operations import (
    CorpusReleaseRecord,
    KnowledgeInitializationAuditRecord,
    KnowledgeInitializationRunRecord,
)
from erp_assistant.operations.initialization_contracts import (
    CandidateBuildDispatcher,
    InitializationCommand,
    InitializationError,
    InitializationMode,
    InitializationRunView,
    InitializationStateView,
    InitializationStatus,
    RuntimeOption,
)
from erp_assistant.operations.initialization_contracts import (
    view as _view,
)
from erp_assistant.operations.initialization_runtime import (
    _option_configuration_fingerprint,
    _runtime_option,
)
from erp_assistant.operations.initialization_runtime import (
    runtime_options as runtime_options,
)
from erp_assistant.operations.initialization_worker import (
    execute_initialization_build as execute_initialization_build,
)
from erp_assistant.operations.release_contracts import ReleaseError, ReleaseStatus
from erp_assistant.operations.releases import CorpusReleaseWorkflow
from erp_assistant.orchestration.providers import (
    embedding_binding_fingerprint,
)
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.index_provisioning import (
    IndexProvisioner,
    IndexProvisioningError,
    IndexSpecification,
)
from erp_assistant.rag.retriever import (
    retrieval_configuration,
    retrieval_configuration_fingerprint,
)
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore

__all__ = [
    "InitializationCommand",
    "InitializationError",
    "InitializationMode",
    "InitializationRunView",
    "InitializationStateView",
    "InitializationStatus",
    "KnowledgeInitialization",
    "RuntimeOption",
    "execute_initialization_build",
    "runtime_options",
]

_NONTERMINAL = {"QUEUED", "RUNNING", "WAITING"}
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


class KnowledgeInitialization:
    """Owns first-run state, Profile provisioning and safe recovery decisions."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        provisioner: IndexProvisioner,
        candidate_dispatcher: CandidateBuildDispatcher | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._provisioner = provisioner
        self._candidate_dispatcher = candidate_dispatcher

    async def status(
        self, classification: DataClassification
    ) -> InitializationStateView:
        run = await self._latest_run(classification)
        if run is not None:
            run = await self._run(run.id, lock=True)
        if run is not None and await self._expire_stale_run(run):
            run = await self._latest_run(classification)
        active_release = await self._session.scalar(
            select(CorpusReleaseRecord.id).where(
                CorpusReleaseRecord.data_classification == classification.value,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
        )
        active_profile = await self._session.scalar(
            select(RetrievalProfileRecord.id).where(
                RetrievalProfileRecord.data_classification == classification.value,
                RetrievalProfileRecord.status == "ACTIVE",
            )
        )
        if active_release is not None:
            action = "MANAGE_RELEASES"
        elif run is not None and run.status == InitializationStatus.SUCCEEDED.value:
            action = "MANAGE_RELEASES"
        elif (
            run is not None
            and run.status == InitializationStatus.FAILED.value
            and not run.retryable
        ):
            action = "INITIALIZE"
        elif run is not None and run.status in _NONTERMINAL | {"FAILED"}:
            action = "CONTINUE_INITIALIZATION"
        elif active_profile is not None:
            action = "MIGRATE"
        else:
            action = "INITIALIZE"
        return InitializationStateView(
            action=action,
            run=_view(run) if run is not None else None,
            runtime_options=runtime_options(self._settings, classification),
        )

    async def start(self, command: InitializationCommand) -> InitializationRunView:
        idempotency_key = command.idempotency_key.strip()
        if not idempotency_key:
            raise InitializationError(
                "IDEMPOTENCY_KEY_REQUIRED", "初始化操作必须提供幂等键"
            )
        existing = await self._session.scalar(
            select(KnowledgeInitializationRunRecord).where(
                KnowledgeInitializationRunRecord.data_classification
                == command.classification.value,
                KnowledgeInitializationRunRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return _view(existing)
        owner = await self._nonterminal_run(command.classification)
        if owner is not None:
            raise InitializationError(
                "INITIALIZATION_ALREADY_RUNNING",
                "该数据分类已有可恢复的初始化运行",
                run_id=owner.id,
            )
        active_release = await self._session.scalar(
            select(CorpusReleaseRecord.id).where(
                CorpusReleaseRecord.data_classification == command.classification.value,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
        )
        if active_release is not None:
            raise InitializationError(
                "FRESH_INITIALIZATION_NOT_AVAILABLE",
                "当前分类已有活动 Release，请使用知识发布治理",
            )
        active_profile = await self._session.scalar(
            select(RetrievalProfileRecord.id).where(
                RetrievalProfileRecord.data_classification
                == command.classification.value,
                RetrievalProfileRecord.status == "ACTIVE",
            )
        )
        latest = await self._latest_run(command.classification)
        if (
            latest is not None
            and latest.status == InitializationStatus.FAILED.value
            and latest.retryable
        ):
            raise InitializationError(
                "INITIALIZATION_RETRY_REQUIRED",
                "该数据分类存在可重试的失败运行，请先恢复原运行",
                run_id=latest.id,
                retryable=True,
            )
        migration_abandoned = bool(
            latest is not None
            and latest.mode == InitializationMode.MIGRATE_ACTIVE.value
            and latest.status == InitializationStatus.FAILED.value
            and not latest.retryable
        )
        if active_profile is not None and not migration_abandoned:
            raise InitializationError(
                "MIGRATION_REQUIRED",
                "当前分类存在历史 ACTIVE Profile，请先执行无重建迁移",
            )
        if (
            latest is not None
            and latest.mode == InitializationMode.FRESH.value
            and latest.status == InitializationStatus.FAILED.value
            and not latest.retryable
            and latest.profile_id is not None
        ):
            abandoned = await self._session.get(
                RetrievalProfileRecord,
                latest.profile_id,
                with_for_update=True,
            )
            if abandoned is not None and abandoned.status == "STAGING":
                abandoned.status = "RETIRED"
                latest.revision += 1
                latest.updated_at = datetime.now(UTC)
                self._audit(
                    latest,
                    "ABANDON",
                    command.actor_id,
                    "FAILED_INITIALIZATION_ABANDONED",
                    command.trace_id,
                )
        if await self._has_blocking_staging_profile(command.classification):
            raise InitializationError(
                "FRESH_INITIALIZATION_NOT_AVAILABLE",
                "当前状态应恢复初始化或迁移已有 Profile",
            )
        option = _runtime_option(
            self._settings, command.classification, command.runtime_key
        )
        now = datetime.now(UTC)
        run = KnowledgeInitializationRunRecord(
            id=uuid4(),
            data_classification=command.classification.value,
            mode=InitializationMode.FRESH.value,
            stage="PREFLIGHT",
            status=InitializationStatus.RUNNING.value,
            attempt=1,
            idempotency_key=idempotency_key,
            runtime_key=option.key,
            actor_id=command.actor_id,
            trace_id=command.trace_id,
            retryable=False,
            progress=5,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        self._audit(run, "START", command.actor_id, "INITIALIZATION_STARTED", command.trace_id)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self._session.scalar(
                select(KnowledgeInitializationRunRecord).where(
                    KnowledgeInitializationRunRecord.data_classification
                    == command.classification.value,
                    KnowledgeInitializationRunRecord.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return _view(existing)
            owner = await self._nonterminal_run(command.classification)
            raise InitializationError(
                "INITIALIZATION_ALREADY_RUNNING",
                "该数据分类已有可恢复的初始化运行",
                run_id=owner.id if owner else None,
            ) from exc
        if not await self._preflight(run, option):
            return _view(run)
        return await self._provision(run, option)

    async def migrate_active(
        self,
        classification: DataClassification,
        *,
        idempotency_key: str,
        actor_id: str,
        trace_id: UUID,
    ) -> InitializationRunView:
        existing = await self._session.scalar(
            select(KnowledgeInitializationRunRecord).where(
                KnowledgeInitializationRunRecord.data_classification
                == classification.value,
                KnowledgeInitializationRunRecord.idempotency_key
                == idempotency_key.strip(),
            )
        )
        if existing is not None:
            return _view(existing)
        owner = await self._nonterminal_run(classification)
        if owner is not None:
            raise InitializationError(
                "INITIALIZATION_ALREADY_RUNNING",
                "该数据分类已有可恢复的初始化运行",
                run_id=owner.id,
            )
        active_release = await self._session.scalar(
            select(CorpusReleaseRecord.id).where(
                CorpusReleaseRecord.data_classification == classification.value,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
        )
        if active_release is not None:
            raise InitializationError(
                "MIGRATION_NOT_AVAILABLE",
                "当前分类已有活动 Release，请使用知识发布治理",
            )
        profile = await self._session.scalar(
            select(RetrievalProfileRecord).where(
                RetrievalProfileRecord.data_classification == classification.value,
                RetrievalProfileRecord.status == "ACTIVE",
            ).with_for_update()
        )
        if profile is None:
            raise InitializationError(
                "MIGRATION_PROFILE_NOT_FOUND", "没有可迁移的 ACTIVE Profile"
            )
        now = datetime.now(UTC)
        run = KnowledgeInitializationRunRecord(
            id=uuid4(),
            data_classification=classification.value,
            mode=InitializationMode.MIGRATE_ACTIVE.value,
            stage="LEGACY_VALIDATION",
            status=InitializationStatus.RUNNING.value,
            attempt=1,
            idempotency_key=idempotency_key.strip(),
            configuration_fingerprint=profile.configuration_fingerprint,
            profile_id=profile.id,
            actor_id=actor_id,
            trace_id=trace_id,
            retryable=False,
            progress=40,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        self._audit(run, "MIGRATE", actor_id, "MIGRATION_STARTED", trace_id)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self._session.scalar(
                select(KnowledgeInitializationRunRecord).where(
                    KnowledgeInitializationRunRecord.data_classification
                    == classification.value,
                    KnowledgeInitializationRunRecord.idempotency_key
                    == idempotency_key.strip(),
                )
            )
            if existing is not None:
                return _view(existing)
            owner = await self._nonterminal_run(classification)
            raise InitializationError(
                "INITIALIZATION_ALREADY_RUNNING",
                "该数据分类已有可恢复的初始化运行",
                run_id=owner.id if owner else None,
            ) from exc
        run_id = run.id
        try:
            specification = self._legacy_index_specification(profile)
            await asyncio.to_thread(self._provisioner.reconcile_legacy, specification)
            run = await self._run(run_id, lock=True)
            if run.status != InitializationStatus.RUNNING.value:
                return _view(run)
            release = await CorpusReleaseWorkflow(self._session).bootstrap(
                profile.id,
                actor_id=actor_id,
                trace_id=trace_id,
                commit=False,
            )
        except (IndexProvisioningError, ReleaseError) as exc:
            await self._session.rollback()
            run = await self._run(run_id, lock=True)
            if run.status == InitializationStatus.CANCELLED.value:
                return _view(run)
            run.status = InitializationStatus.FAILED.value
            run.failure_code = exc.code
            run.failure_message = exc.message
            run.retryable = bool(getattr(exc, "retryable", False))
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", actor_id, exc.code, trace_id)
            await self._session.commit()
            return _view(run)
        except IntegrityError:
            await self._session.rollback()
            run = await self._run(run_id, lock=True)
            if run.status == InitializationStatus.CANCELLED.value:
                return _view(run)
            run.status = InitializationStatus.FAILED.value
            run.failure_code = "MIGRATION_CONFLICT"
            run.failure_message = "历史 Profile 迁移与其他发布操作发生冲突"
            run.retryable = True
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", actor_id, run.failure_code, trace_id)
            await self._session.commit()
            return _view(run)
        run = await self._run(run_id, lock=True)
        run.candidate_release_id = release.id
        run.stage = "RELEASE_ACTIVE"
        run.status = InitializationStatus.SUCCEEDED.value
        run.progress = 100
        run.revision += 1
        run.updated_at = datetime.now(UTC)
        self._audit(run, "MIGRATE", actor_id, "MIGRATION_SUCCEEDED", trace_id)
        await self._session.commit()
        return _view(run)

    async def continue_to_candidate(
        self,
        run_id: UUID,
        *,
        release_version: str,
        actor_id: str,
        trace_id: UUID,
        expected_revision: int,
    ) -> InitializationRunView:
        run = await self._run(run_id, lock=True)
        self._check_revision(run, expected_revision)
        if (
            run.status != InitializationStatus.WAITING.value
            or run.stage != "WAITING_FOR_CONTENT"
            or run.profile_id is None
        ):
            raise InitializationError(
                "INITIALIZATION_NOT_WAITING_FOR_CONTENT",
                "当前初始化运行不能冻结候选语料",
            )
        try:
            starting_revision = run.revision
            release = await CorpusReleaseWorkflow(self._session).create_candidate(
                release_version,
                run.profile_id,
                actor_id=actor_id,
                trace_id=trace_id,
                commit=False,
            )
        except ReleaseError as exc:
            raise InitializationError(exc.code, exc.message) from exc
        run = await self._run(run.id, lock=True)
        if (
            run.status != InitializationStatus.WAITING.value
            or run.revision != starting_revision
        ):
            candidate = await self._session.get(CorpusReleaseRecord, release.id)
            if candidate is not None:
                candidate.status = ReleaseStatus.FAILED.value
                candidate.failure_reason_code = "INITIALIZATION_INTERRUPTED"
                candidate.updated_at = datetime.now(UTC)
                await self._session.commit()
            raise InitializationError(
                "INITIALIZATION_REVISION_CONFLICT",
                "初始化状态已更新，请刷新后重试",
                run_id=run.id,
            )
        run.candidate_release_id = release.id
        run.stage = "CANDIDATE_BUILD"
        run.status = InitializationStatus.QUEUED.value
        run.progress = 70
        task_id = str(uuid4())
        run.lease_owner = f"task:{task_id}"
        run.lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=self._settings.knowledge_index_lease_seconds
        )
        run.revision += 1
        run.actor_id = actor_id
        run.trace_id = trace_id
        run.updated_at = datetime.now(UTC)
        self._audit(run, "CONTINUE", actor_id, "CANDIDATE_FROZEN", trace_id)
        await self._session.commit()
        await self._dispatch_candidate(run, task_id)
        return _view(run)

    async def retry(
        self,
        run_id: UUID,
        *,
        actor_id: str,
        trace_id: UUID,
        expected_revision: int,
    ) -> InitializationRunView:
        run = await self._run(run_id, lock=True)
        self._check_revision(run, expected_revision)
        if run.status != InitializationStatus.FAILED.value or not run.retryable:
            raise InitializationError(
                "INITIALIZATION_NOT_RETRYABLE", "当前初始化运行不可重试"
            )
        try:
            option = _runtime_option(
                self._settings,
                DataClassification(run.data_classification),
                cast(str, run.runtime_key),
            )
        except InitializationError as exc:
            if exc.code != "RUNTIME_DEPLOYMENT_UNAVAILABLE":
                raise
            await self._abandon_failed_run(
                run,
                actor_id=actor_id,
                trace_id=trace_id,
                failure_code=exc.code,
                failure_message="初始化 Runtime 已不可用，请开始新的初始化运行",
            )
            return _view(run)
        profile = (
            await self._session.get(RetrievalProfileRecord, run.profile_id)
            if run.profile_id is not None
            else None
        )
        if (
            profile is not None
            and run.configuration_fingerprint is not None
            and _option_configuration_fingerprint(option, profile)
            != run.configuration_fingerprint
        ):
            await self._abandon_failed_run(
                run,
                actor_id=actor_id,
                trace_id=trace_id,
                failure_code="RUNTIME_CONFIGURATION_CHANGED",
                failure_message="初始化 Runtime 配置已变化，请开始新的初始化运行",
            )
            return _view(run)
        if run.candidate_release_id is not None:
            task_id = str(uuid4())
            run.status = InitializationStatus.QUEUED.value
            run.failure_code = None
            run.failure_message = None
            run.retryable = False
            run.lease_owner = f"task:{task_id}"
            run.lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=self._settings.knowledge_index_lease_seconds
            )
            run.attempt += 1
            run.revision += 1
            run.actor_id = actor_id
            run.trace_id = trace_id
            run.updated_at = datetime.now(UTC)
            self._audit(run, "RETRY", actor_id, "CANDIDATE_BUILD_REQUEUED", trace_id)
            await self._session.commit()
            await self._dispatch_candidate(run, task_id)
            return _view(run)
        run.status = InitializationStatus.RUNNING.value
        run.failure_code = None
        run.failure_message = None
        run.retryable = False
        run.attempt += 1
        run.revision += 1
        run.actor_id = actor_id
        run.trace_id = trace_id
        run.updated_at = datetime.now(UTC)
        self._audit(run, "RETRY", actor_id, "INITIALIZATION_RETRIED", trace_id)
        await self._session.commit()
        if not await self._preflight(run, option):
            return _view(run)
        return await self._provision(run, option)

    async def cancel(
        self,
        run_id: UUID,
        *,
        actor_id: str,
        trace_id: UUID,
        expected_revision: int,
    ) -> InitializationRunView:
        run = await self._run(run_id, lock=True)
        self._check_revision(run, expected_revision)
        if run.mode == InitializationMode.MIGRATE_ACTIVE.value:
            raise InitializationError(
                "INITIALIZATION_NOT_CANCELLABLE",
                "历史 Profile 迁移是原子操作，不能在提交过程中取消",
            )
        if run.status not in _NONTERMINAL:
            raise InitializationError(
                "INITIALIZATION_NOT_CANCELLABLE", "当前初始化运行不可取消"
            )
        run.status = InitializationStatus.CANCELLED.value
        run.retryable = False
        run.revision += 1
        run.actor_id = actor_id
        run.trace_id = trace_id
        run.updated_at = datetime.now(UTC)
        if run.candidate_release_id is not None:
            release = await self._session.get(
                CorpusReleaseRecord,
                run.candidate_release_id,
                with_for_update=True,
            )
            if release is not None and release.status in {
                ReleaseStatus.BUILDING.value,
                ReleaseStatus.EVALUATING.value,
                ReleaseStatus.READY.value,
            }:
                release.status = ReleaseStatus.FAILED.value
                release.failure_reason_code = "INITIALIZATION_CANCELLED"
                release.updated_at = datetime.now(UTC)
        self._audit(run, "CANCEL", actor_id, "INITIALIZATION_CANCELLED", trace_id)
        await self._session.commit()
        return _view(run)

    async def _provision(
        self,
        run: KnowledgeInitializationRunRecord,
        option: RuntimeOption,
    ) -> InitializationRunView:
        try:
            profile = (
                await self._session.get(RetrievalProfileRecord, run.profile_id)
                if run.profile_id
                else None
            )
            if profile is None:
                profile = await self._new_profile(run, option)
                run.profile_id = profile.id
                run.stage = "PROFILE_PROVISION"
                run.progress = 25
                run.revision += 1
                run.updated_at = datetime.now(UTC)
                await self._session.commit()
            run.stage = "INDEX_PROVISION"
            run.progress = 50
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            await self._session.commit()
            provisioning_revision = run.revision
            specification = IndexSpecification(
                uri=option.vector_uri,
                physical_locator=cast(str, profile.physical_index_locator),
                dimension=profile.embedding_dimension,
                embedding_binding_fingerprint=cast(
                    str, profile.embedding_binding_fingerprint
                ),
                schema_fingerprint=cast(str, profile.index_schema_fingerprint),
                index_identity=profile.index_identity,
            )
            observed = await asyncio.to_thread(self._provisioner.provision, specification)
            run = await self._run(run.id, lock=True)
            if (
                run.status != InitializationStatus.RUNNING.value
                or run.stage != "INDEX_PROVISION"
                or run.revision != provisioning_revision
            ):
                return _view(run)
            if (
                observed.physical_locator != specification.physical_locator
                or observed.schema_fingerprint != specification.schema_fingerprint
                or observed.embedding_binding_fingerprint
                != specification.embedding_binding_fingerprint
                or observed.index_identity != specification.index_identity
            ):
                raise IndexProvisioningError(
                    "INDEX_IDENTITY_COLLISION",
                    "物理索引的实际身份与初始化候选不一致",
                    retryable=False,
                )
            run.stage = "WAITING_FOR_CONTENT"
            run.status = InitializationStatus.WAITING.value
            run.progress = 60
            run.failure_code = None
            run.failure_message = None
            run.retryable = False
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(
                run,
                "PROVISION",
                run.actor_id,
                "INDEX_PROVISIONED",
                run.trace_id,
            )
            await self._session.commit()
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, IndexProvisioningError)
                else IndexProvisioningError(
                    "INDEX_DEPENDENCY_UNAVAILABLE",
                    "物理索引依赖暂时不可用",
                    retryable=True,
                )
            )
            await self._session.rollback()
            run = await self._run(run.id, lock=True)
            if run.status == InitializationStatus.CANCELLED.value:
                return _view(run)
            run.status = InitializationStatus.FAILED.value
            run.failure_code = exc.code
            run.failure_message = exc.message
            run.retryable = exc.retryable
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", run.actor_id, exc.code, run.trace_id)
            await self._session.commit()
        return _view(run)

    async def _new_profile(
        self,
        run: KnowledgeInitializationRunRecord,
        option: RuntimeOption,
    ) -> RetrievalProfileRecord:
        profile_id = uuid4()
        binding_fingerprint = embedding_binding_fingerprint(option.embedding)
        token = profile_id.hex[:12]
        safe_base = _SAFE_NAME.sub("_", option.collection_base).strip("_")
        schema_fingerprint = IndexSpecification.create(
            uri=option.vector_uri,
            physical_locator="pending",
            dimension=option.embedding_dimension,
            embedding_binding_fingerprint=binding_fingerprint,
            index_identity="pending",
        ).schema_fingerprint
        physical_locator = (
            f"{safe_base}_v3_{option.classification.value.casefold()}_"
            f"d{option.embedding_dimension}_e{binding_fingerprint[:10]}_"
            f"s{schema_fingerprint[:10]}_p{token}"
        )
        vector_store = MilvusVectorStore(
            uri=option.vector_uri,
            collection=option.collection_base,
            dimension=option.embedding_dimension,
            embedding_binding=binding_fingerprint,
            physical_collection=physical_locator,
        )
        config = retrieval_configuration(
            settings=option.runtime_settings,
            vector_store=vector_store,
            embedding=option.embedding,
            reranker=option.reranker,
        )
        specification = IndexSpecification.create(
            uri=option.vector_uri,
            physical_locator=physical_locator,
            dimension=option.embedding_dimension,
            embedding_binding_fingerprint=binding_fingerprint,
            index_identity=vector_store.identity,
        )
        profile_key = f"{option.classification.value.casefold()}-initialized"
        maximum = await self._session.scalar(
            select(func.max(RetrievalProfileRecord.version)).where(
                RetrievalProfileRecord.profile_key == profile_key
            )
        )
        profile = RetrievalProfileRecord(
            id=profile_id,
            profile_key=profile_key,
            version=int(maximum or 0) + 1,
            data_classification=option.classification.value,
            provider_boundary=option.provider_boundary.value,
            embedding_deployment=option.embedding_deployment,
            embedding_model=option.embedding_model,
            embedding_dimension=option.embedding_dimension,
            rerank_deployment=option.rerank_deployment,
            rerank_model=option.rerank_model,
            index_identity=vector_store.identity,
            physical_index_locator=physical_locator,
            index_schema_fingerprint=specification.schema_fingerprint,
            embedding_binding_fingerprint=binding_fingerprint,
            parser_version=option.parser_version,
            chunk_version=option.chunk_version,
            retrieval_config=config,
            configuration_fingerprint=retrieval_configuration_fingerprint(config),
            production_ready=option.production_ready,
            status="STAGING",
        )
        run.configuration_fingerprint = profile.configuration_fingerprint
        self._session.add(profile)
        return profile

    async def _latest_run(
        self, classification: DataClassification
    ) -> KnowledgeInitializationRunRecord | None:
        return cast(
            KnowledgeInitializationRunRecord | None,
            await self._session.scalar(
                select(KnowledgeInitializationRunRecord)
                .where(
                    KnowledgeInitializationRunRecord.data_classification
                    == classification.value
                )
                .order_by(
                    KnowledgeInitializationRunRecord.updated_at.desc(),
                    KnowledgeInitializationRunRecord.id.desc(),
                )
                .limit(1)
            ),
        )

    async def _nonterminal_run(
        self, classification: DataClassification
    ) -> KnowledgeInitializationRunRecord | None:
        return cast(
            KnowledgeInitializationRunRecord | None,
            await self._session.scalar(
                select(KnowledgeInitializationRunRecord).where(
                    KnowledgeInitializationRunRecord.data_classification
                    == classification.value,
                    KnowledgeInitializationRunRecord.status.in_(_NONTERMINAL),
                )
            ),
        )

    async def _has_blocking_staging_profile(
        self, classification: DataClassification
    ) -> bool:
        staging_ids = tuple(
            (
                await self._session.scalars(
                    select(RetrievalProfileRecord.id).where(
                        RetrievalProfileRecord.data_classification
                        == classification.value,
                        RetrievalProfileRecord.status == "STAGING",
                    )
                )
            ).all()
        )
        for profile_id in staging_ids:
            statuses = tuple(
                (
                    await self._session.scalars(
                        select(KnowledgeInitializationRunRecord.status).where(
                            KnowledgeInitializationRunRecord.profile_id == profile_id
                        )
                    )
                ).all()
            )
            if not statuses or any(
                status
                not in {
                    InitializationStatus.CANCELLED.value,
                    InitializationStatus.FAILED.value,
                }
                for status in statuses
            ):
                return True
        return False

    async def _dispatch_candidate(
        self,
        run: KnowledgeInitializationRunRecord,
        task_id: str,
    ) -> None:
        if self._candidate_dispatcher is None:
            return
        dispatch_revision = run.revision
        try:
            observed_task_id = self._candidate_dispatcher(
                run.id,
                run.trace_id,
                task_id,
            )
            if observed_task_id != task_id:
                raise RuntimeError("Initialization dispatcher changed task identity")
        except Exception as exc:
            run = await self._run(run.id, lock=True)
            if (
                run.status != InitializationStatus.QUEUED.value
                or run.revision != dispatch_revision
            ):
                return
            run.status = InitializationStatus.FAILED.value
            run.failure_code = "INITIALIZATION_DISPATCH_FAILED"
            run.failure_message = "候选构建任务无法入队"
            run.retryable = True
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", run.actor_id, run.failure_code, run.trace_id)
            await self._session.commit()
            raise InitializationError(
                run.failure_code,
                run.failure_message,
                run_id=run.id,
                retryable=True,
            ) from exc
        run = await self._run(run.id, lock=True)
        if (
            run.status != InitializationStatus.QUEUED.value
            or run.revision != dispatch_revision
        ):
            return
        await self._session.commit()

    async def _preflight(
        self,
        run: KnowledgeInitializationRunRecord,
        option: RuntimeOption,
    ) -> bool:
        if self._settings.app_env.casefold() not in {"prod", "production"}:
            return True
        if not option.production_ready:
            run.status = InitializationStatus.FAILED.value
            run.failure_code = "PRODUCTION_RUNTIME_REQUIRED"
            run.failure_message = "生产初始化只能使用验证通过的 Runtime Deployment"
            run.retryable = False
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", run.actor_id, run.failure_code, run.trace_id)
            await self._session.commit()
            return False
        from erp_assistant.operations.production_probes import (
            validate_production_runtime_startup,
        )

        preflight_revision = run.revision
        try:
            await validate_production_runtime_startup(
                self._settings,
                observed_at=datetime.now(UTC),
            )
        except Exception as exc:
            run = await self._run(run.id, lock=True)
            if (
                run.status != InitializationStatus.RUNNING.value
                or run.revision != preflight_revision
            ):
                return False
            failed_checks = tuple(getattr(exc, "failed_checks", ()))
            run.status = InitializationStatus.FAILED.value
            run.failure_code = "INITIALIZATION_PREFLIGHT_FAILED"
            run.failure_message = (
                f"生产依赖预检未通过：{', '.join(map(str, failed_checks))}"
                if failed_checks
                else "生产依赖预检未通过，请检查依赖状态后重试"
            )
            run.retryable = True
            run.revision += 1
            run.updated_at = datetime.now(UTC)
            self._audit(run, "FAIL", run.actor_id, run.failure_code, run.trace_id)
            await self._session.commit()
            return False
        run = await self._run(run.id, lock=True)
        if (
            run.status != InitializationStatus.RUNNING.value
            or run.revision != preflight_revision
        ):
            return False
        run.stage = "PREFLIGHT"
        run.progress = 15
        run.revision += 1
        run.updated_at = datetime.now(UTC)
        await self._session.commit()
        return True

    async def _expire_stale_run(
        self, run: KnowledgeInitializationRunRecord
    ) -> bool:
        if (
            run.status not in {
                InitializationStatus.QUEUED.value,
                InitializationStatus.RUNNING.value,
            }
            or run.stage != "CANDIDATE_BUILD"
            or run.lease_expires_at is None
        ):
            return False
        expires_at = (
            run.lease_expires_at
            if run.lease_expires_at.tzinfo is not None
            else run.lease_expires_at.replace(tzinfo=UTC)
        )
        if expires_at > datetime.now(UTC):
            return False
        was_queued = run.status == InitializationStatus.QUEUED.value
        run.status = InitializationStatus.FAILED.value
        if was_queued:
            run.failure_code = "INITIALIZATION_DISPATCH_EXPIRED"
            run.failure_message = "候选构建任务未在期限内被 Worker 领取"
        else:
            run.failure_code = "INITIALIZATION_WORKER_LEASE_EXPIRED"
            run.failure_message = "候选构建 Worker 心跳已过期"
        run.retryable = True
        run.revision += 1
        run.updated_at = datetime.now(UTC)
        self._audit(run, "FAIL", run.actor_id, run.failure_code, run.trace_id)
        await self._session.commit()
        return True

    async def _run(
        self, run_id: UUID, *, lock: bool
    ) -> KnowledgeInitializationRunRecord:
        statement = select(KnowledgeInitializationRunRecord).where(
            KnowledgeInitializationRunRecord.id == run_id
        )
        if lock:
            statement = statement.with_for_update()
        run = await self._session.scalar(
            statement.execution_options(populate_existing=True)
        )
        if run is None:
            raise InitializationError("INITIALIZATION_NOT_FOUND", "初始化运行不存在")
        return run

    def _legacy_index_specification(
        self, profile: RetrievalProfileRecord
    ) -> IndexSpecification:
        try:
            binding = build_retrieval_runtime_registry(self._settings).binding_for(profile)
        except (RagDependencyError, ValueError) as exc:
            raise IndexProvisioningError(
                "LEGACY_RUNTIME_BINDING_UNAVAILABLE",
                "已有 Profile 没有唯一匹配的 Runtime 绑定",
                retryable=False,
            ) from exc
        store = binding.vector_store
        if not isinstance(store, MilvusVectorStore):
            raise IndexProvisioningError(
                "LEGACY_RUNTIME_BINDING_UNAVAILABLE",
                "已有 Profile 的 Runtime 未绑定 Milvus 物理索引",
                retryable=False,
            )
        binding_fingerprint = embedding_binding_fingerprint(binding.embedding)
        expected = IndexSpecification.create(
            uri=store.uri,
            physical_locator=store.collection,
            dimension=profile.embedding_dimension,
            embedding_binding_fingerprint=binding_fingerprint,
            index_identity=store.identity,
        )
        persisted = (
            (profile.physical_index_locator, expected.physical_locator),
            (profile.index_schema_fingerprint, expected.schema_fingerprint),
            (
                profile.embedding_binding_fingerprint,
                expected.embedding_binding_fingerprint,
            ),
            (profile.index_identity, expected.index_identity),
        )
        if any(value is not None and value != canonical for value, canonical in persisted):
            raise IndexProvisioningError(
                "LEGACY_INDEX_IDENTITY_COLLISION",
                "已有 Profile 的索引身份与 Runtime 绑定不一致",
                retryable=False,
            )
        profile.physical_index_locator = expected.physical_locator
        profile.index_schema_fingerprint = expected.schema_fingerprint
        profile.embedding_binding_fingerprint = expected.embedding_binding_fingerprint
        profile.index_identity = expected.index_identity
        return expected

    @staticmethod
    def _check_revision(
        run: KnowledgeInitializationRunRecord, expected_revision: int
    ) -> None:
        if run.revision != expected_revision:
            raise InitializationError(
                "INITIALIZATION_REVISION_CONFLICT", "初始化状态已更新，请刷新后重试"
            )

    async def _abandon_failed_run(
        self,
        run: KnowledgeInitializationRunRecord,
        *,
        actor_id: str,
        trace_id: UUID,
        failure_code: str,
        failure_message: str,
    ) -> None:
        profile = (
            await self._session.get(
                RetrievalProfileRecord,
                run.profile_id,
                with_for_update=True,
            )
            if run.profile_id is not None
            else None
        )
        if profile is not None and profile.status == "STAGING":
            profile.status = "RETIRED"
        if run.candidate_release_id is not None:
            release = await self._session.get(
                CorpusReleaseRecord,
                run.candidate_release_id,
                with_for_update=True,
            )
            if release is not None and release.status in {
                ReleaseStatus.BUILDING.value,
                ReleaseStatus.EVALUATING.value,
                ReleaseStatus.READY.value,
            }:
                release.status = ReleaseStatus.FAILED.value
                release.failure_reason_code = "INITIALIZATION_ABANDONED"
                release.updated_at = datetime.now(UTC)
        run.failure_code = failure_code
        run.failure_message = failure_message
        run.retryable = False
        run.revision += 1
        run.actor_id = actor_id
        run.trace_id = trace_id
        run.updated_at = datetime.now(UTC)
        self._audit(run, "ABANDON", actor_id, failure_code, trace_id)
        await self._session.commit()

    def _audit(
        self,
        run: KnowledgeInitializationRunRecord,
        action: str,
        actor_id: str,
        reason_code: str,
        trace_id: UUID,
    ) -> None:
        self._session.add(
            KnowledgeInitializationAuditRecord(
                id=uuid4(),
                run_id=run.id,
                action=action,
                actor_id=actor_id,
                reason_code=reason_code,
                trace_id=trace_id,
                created_at=datetime.now(UTC),
            )
        )
