from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.models.operations import KnowledgeInitializationRunRecord
from erp_assistant.rag.document_indexer import (
    DocumentIndexer,
    DocumentIndexOutcome,
    DocumentIndexRetryableError,
)
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.knowledge_corpus_contracts import CorpusProcessor
from erp_assistant.rag.runtime_registry import (
    RetrievalRuntimeBinding,
    RetrievalRuntimeRegistry,
)
from erp_assistant.rag.types import DataClassification


class ClassifiedDocumentIndexer:
    """Select an indexing route from server-owned document classification."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        routes: dict[DataClassification, Callable[[], CorpusProcessor]],
    ) -> None:
        self._sessions = session_factory
        self._routes = dict(routes)

    async def index(
        self, document_id: UUID, *, trace_id: UUID | None = None
    ) -> DocumentIndexOutcome:
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
        if document is None:
            raise LookupError(f"Knowledge document {document_id} does not exist")
        try:
            classification = DataClassification(document.data_classification)
        except ValueError as exc:
            raise RagDependencyError("INVALID_DATA_CLASSIFICATION", "文档数据分类无效") from exc
        factory = self._routes.get(classification)
        if factory is None:
            code = (
                "RESTRICTED_ROUTE_UNAVAILABLE"
                if classification is DataClassification.RESTRICTED
                else "INDEX_ROUTE_UNAVAILABLE"
            )
            raise RagDependencyError(code, "没有可用于该数据分类的批准索引路由")
        try:
            indexer = factory()
        except (TypeError, ValueError) as exc:
            code = (
                "RESTRICTED_ROUTE_UNAVAILABLE"
                if classification is DataClassification.RESTRICTED
                else "INDEX_ROUTE_UNAVAILABLE"
            )
            raise RagDependencyError(code, "索引路由配置不完整") from exc
        return await indexer.index(document_id, trace_id=trace_id)


class ActiveProfileDocumentIndexer:
    """Route writes through ACTIVE, or the owned first-run STAGING profile."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: RetrievalRuntimeRegistry,
        indexer_factory: Callable[[RetrievalRuntimeBinding], DocumentIndexer],
    ) -> None:
        self._sessions = session_factory
        self._registry = registry
        self._indexer_factory = indexer_factory

    async def index(
        self, document_id: UUID, *, trace_id: UUID | None = None
    ) -> DocumentIndexOutcome:
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise LookupError(f"Knowledge document {document_id} does not exist")
            classification = document.data_classification
            profile = await session.scalar(
                select(RetrievalProfileRecord)
                .join(
                    KnowledgeInitializationRunRecord,
                    KnowledgeInitializationRunRecord.profile_id
                    == RetrievalProfileRecord.id,
                )
                .where(
                    RetrievalProfileRecord.data_classification == classification,
                    RetrievalProfileRecord.status == "STAGING",
                    KnowledgeInitializationRunRecord.status == "WAITING",
                    KnowledgeInitializationRunRecord.stage == "WAITING_FOR_CONTENT",
                )
                .order_by(
                    KnowledgeInitializationRunRecord.updated_at.desc(),
                    KnowledgeInitializationRunRecord.id.desc(),
                )
                .limit(1)
            )
            if profile is None:
                profile = await session.scalar(
                    select(RetrievalProfileRecord).where(
                        RetrievalProfileRecord.data_classification == classification,
                        RetrievalProfileRecord.status == "ACTIVE",
                    )
                )
        if profile is None:
            raise RagDependencyError(
                "INDEX_PROFILE_UNAVAILABLE",
                "没有可用于文档处理的活动或初始化检索 Profile",
            )
        binding = self._registry.binding_for(profile)
        outcome = await self._indexer_factory(binding).index(document_id, trace_id=trace_id)
        async with self._sessions() as session:
            current = await session.get(RetrievalProfileRecord, profile.id)
            still_owned = bool(
                current is not None
                and current.data_classification == classification
                and (
                    current.status == "ACTIVE"
                    or (
                        current.status == "STAGING"
                        and await session.scalar(
                            select(KnowledgeInitializationRunRecord.id).where(
                                KnowledgeInitializationRunRecord.profile_id == current.id,
                                KnowledgeInitializationRunRecord.status == "WAITING",
                                KnowledgeInitializationRunRecord.stage
                                == "WAITING_FOR_CONTENT",
                            )
                        )
                        is not None
                    )
                )
            )
        if not still_owned:
            raise DocumentIndexRetryableError(
                "INDEX_PROFILE_CHANGED",
                "检索 Profile 在索引期间发生切换，可安全重试",
                outcome.attempt,
            )
        return outcome
