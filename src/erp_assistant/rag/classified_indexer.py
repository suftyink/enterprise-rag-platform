from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.document_indexer import DocumentIndexOutcome
from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.knowledge_corpus_contracts import CorpusProcessor
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

    async def index(self, document_id: UUID) -> DocumentIndexOutcome:
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
        return await indexer.index(document_id)
