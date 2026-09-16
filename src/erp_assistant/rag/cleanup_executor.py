import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.infrastructure.object_storage import ObjectStorage
from erp_assistant.models.knowledge import KnowledgeCleanupRequest, KnowledgeDocument
from erp_assistant.rag.document_indexer import VectorWriter
from erp_assistant.rag.knowledge_corpus import (
    SYSTEM_CORPUS_ACTOR,
    CorpusError,
    KnowledgeCorpus,
    LifecycleAction,
)
from erp_assistant.rag.types import CleanupStatus, DataClassification


class CleanupDecision(StrEnum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class CleanupOutcome:
    document_id: UUID
    decision: CleanupDecision
    request_id: UUID | None

    def to_dict(self) -> dict[str, object]:
        return {
            "documentId": str(self.document_id),
            "decision": self.decision.value,
            "requestId": str(self.request_id) if self.request_id is not None else None,
        }


class CleanupRetryableError(Exception):
    pass


class KnowledgeCleanupExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        vectors: VectorWriter,
        vectors_by_classification: dict[
            DataClassification, VectorWriter | tuple[VectorWriter, ...]
        ]
        | None = None,
        corpus: KnowledgeCorpus,
    ) -> None:
        self._sessions = session_factory
        self._storage = storage
        self._vectors = vectors
        self._classified_vectors = dict(vectors_by_classification or {})
        self._corpus = corpus

    async def execute(self, document_id: UUID) -> CleanupOutcome:
        request = await self._claim(document_id)
        if request is None:
            return CleanupOutcome(document_id, CleanupDecision.SKIPPED, None)
        try:
            vectors = await self._vector_writers(document_id)
            for artifact in request.artifacts:
                if artifact["kind"] == "vector_document":
                    index_identity = artifact.get("index_identity")
                    if index_identity is not None:
                        try:
                            selected_writers: tuple[VectorWriter, ...] = (
                                vectors[index_identity],
                            )
                        except KeyError as exc:
                            raise CleanupRetryableError(
                                f"向量索引 {index_identity} 没有可用的精确清理路由"
                            ) from exc
                    else:
                        selected_writers = tuple(vectors.values())
                    for writer in selected_writers:
                        await asyncio.to_thread(
                            writer.delete_document,
                            UUID(artifact["document_id"]),
                        )
                elif artifact["kind"] == "object":
                    await asyncio.to_thread(
                        self._storage.delete_object,
                        artifact["object_key"],
                    )
            await self._complete(document_id, LifecycleAction.COMPLETE_CLEANUP, "cleanup completed")
        except Exception as exc:
            try:
                await self._complete(
                    document_id,
                    LifecycleAction.MARK_CLEANUP_FAILED,
                    f"cleanup dependency failed: {exc.__class__.__name__}",
                )
            except CorpusError:
                pass
            raise CleanupRetryableError("知识产物清理失败，可安全重试") from exc
        return CleanupOutcome(document_id, CleanupDecision.COMPLETED, request.id)

    async def _vector_writers(self, document_id: UUID) -> dict[str, VectorWriter]:
        if not self._classified_vectors:
            return {self._vectors.identity: self._vectors}
        async with self._sessions() as session:
            classification = await session.scalar(
                select(KnowledgeDocument.data_classification).where(
                    KnowledgeDocument.id == document_id
                )
            )
        if classification is None:
            raise CleanupRetryableError("清理目标文档不存在")
        try:
            configured = self._classified_vectors[DataClassification(classification)]
        except (KeyError, ValueError) as exc:
            raise CleanupRetryableError("文档分类没有对应的向量清理路由") from exc
        writers = configured if isinstance(configured, tuple) else (configured,)
        return {writer.identity: writer for writer in writers}

    async def _claim(self, document_id: UUID) -> KnowledgeCleanupRequest | None:
        async with self._sessions() as session:
            async with session.begin():
                request = cast(
                    KnowledgeCleanupRequest | None,
                    await session.scalar(
                        select(KnowledgeCleanupRequest)
                        .where(
                            KnowledgeCleanupRequest.document_id == document_id,
                            KnowledgeCleanupRequest.status.in_(
                                [
                                    CleanupStatus.PENDING.value,
                                    CleanupStatus.FAILED.value,
                                ]
                            ),
                        )
                        .order_by(
                            KnowledgeCleanupRequest.created_at,
                            KnowledgeCleanupRequest.id,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    ),
                )
                if request is None:
                    return None
                request.status = CleanupStatus.RUNNING.value
                request.attempt += 1
                request.error_detail = None
            return request

    async def _complete(
        self,
        document_id: UUID,
        action: LifecycleAction,
        reason: str,
    ) -> None:
        view = await self._corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
        await self._corpus.transition(
            document_id,
            action,
            SYSTEM_CORPUS_ACTOR,
            reason=reason,
            expected_revision=view.revision,
        )
