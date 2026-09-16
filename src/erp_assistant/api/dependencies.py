from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import ActorContext, IdentityAccess, IdentityDenied
from erp_assistant.identity.audit import SqlSecurityAuditSink
from erp_assistant.identity.composition import identity_components
from erp_assistant.infrastructure.database import SessionFactory, get_session
from erp_assistant.orchestration.provider_factory import (
    get_chat_provider_for,
    get_embedding_provider,
    get_rerank_provider,
)
from erp_assistant.orchestration.providers import EmbeddingProvider, RerankProvider
from erp_assistant.rag.knowledge_assistant import KnowledgeAssistant
from erp_assistant.rag.profile_bound_retriever import ProfileBoundRetriever
from erp_assistant.rag.retriever import KnowledgeRetriever
from erp_assistant.rag.runtime_composition import get_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import VectorStore, get_vector_store


def request_trace_id(request: Request) -> UUID:
    existing = getattr(request.state, "identity_trace_id", None)
    if isinstance(existing, UUID):
        return existing
    supplied = request.headers.get("X-Request-ID", "")
    try:
        trace_id = UUID(supplied) if supplied else uuid4()
    except ValueError:
        trace_id = uuid4()
    request.state.identity_trace_id = trace_id
    return trace_id


def get_identity_access(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IdentityAccess:
    try:
        verifier, directory = identity_components(settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "IDENTITY_CONFIGURATION_INVALID", "message": str(exc)},
        ) from exc
    return IdentityAccess(
        verifier=verifier,
        directory=directory,
        audit=SqlSecurityAuditSink(session),
    )


async def get_current_actor(
    request: Request,
    access: Annotated[IdentityAccess, Depends(get_identity_access)],
    authorization: Annotated[str | None, Header()] = None,
) -> ActorContext:
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not credential.strip():
        await access.reject_authentication(
            "AUTHENTICATION_REQUIRED", trace_id=request_trace_id(request)
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "需要有效的身份凭据"},
        )
    try:
        return await access.authenticate(credential.strip(), trace_id=request_trace_id(request))
    except IdentityDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


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
    return ProfileBoundRetriever(
        session,
        retriever,
        settings,
        runtime_registry=(
            get_retrieval_runtime_registry() if settings.retrieval_runtime_deployments else None
        ),
    )


def get_knowledge_assistant(
    retriever: Annotated[ProfileBoundRetriever, Depends(get_knowledge_retriever)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> KnowledgeAssistant:
    return KnowledgeAssistant(
        retriever=retriever,
        chat_routes={
            DataClassification.ORDINARY: get_chat_provider_for(DataClassification.ORDINARY),
            DataClassification.RESTRICTED: get_chat_provider_for(DataClassification.RESTRICTED),
        },
        session_factory=SessionFactory,
        context_token_budget=settings.answer_context_token_budget,
    )
