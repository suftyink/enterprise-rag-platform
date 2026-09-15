from typing import Protocol

from erp_assistant.rag.errors import RagDependencyError
from erp_assistant.rag.retriever import RetrievalOutcome, RetrievalRequest
from erp_assistant.rag.types import DataClassification


class Retriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome: ...


class ClassifiedRetrieverRouter:
    def __init__(self, routes: dict[DataClassification, Retriever]) -> None:
        self._routes = dict(routes)

    async def retrieve(self, request: RetrievalRequest) -> RetrievalOutcome:
        retriever = self._routes.get(request.data_classification)
        if retriever is None:
            code = (
                "RESTRICTED_ROUTE_UNAVAILABLE"
                if request.data_classification is DataClassification.RESTRICTED
                else "RETRIEVAL_ROUTE_UNAVAILABLE"
            )
            raise RagDependencyError(code, "没有可用于该数据分类的批准检索路由")
        return await retriever.retrieve(request)
