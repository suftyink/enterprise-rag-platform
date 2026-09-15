from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.database import get_session
from erp_assistant.orchestration.provider_factory import (
    get_embedding_provider,
    get_rerank_provider,
)
from erp_assistant.orchestration.providers import EmbeddingProvider, RerankProvider
from erp_assistant.rag.profile_bound_retriever import ProfileBoundRetriever
from erp_assistant.rag.retriever import KnowledgeRetriever
from erp_assistant.rag.vector_store import VectorStore, get_vector_store


def get_knowledge_retriever(
    session: Annotated[AsyncSession, Depends(get_session)],
    vector_store: Annotated[VectorStore, Depends(get_vector_store)],
    embedding: Annotated[EmbeddingProvider, Depends(get_embedding_provider)],
    reranker: Annotated[RerankProvider, Depends(get_rerank_provider)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileBoundRetriever:
    retriever = KnowledgeRetriever(
        session=session,
        vector_store=vector_store,
        embedding=embedding,
        reranker=reranker,
        settings=settings,
    )
    return ProfileBoundRetriever(session, retriever, settings)
