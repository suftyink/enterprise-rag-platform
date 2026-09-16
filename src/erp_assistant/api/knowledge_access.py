from fastapi import HTTPException, Request, status

from erp_assistant.api.dependencies import request_trace_id
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityResource,
)
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.types import KnowledgeScope


def knowledge_resource(document: KnowledgeDocument) -> IdentityResource:
    return IdentityResource(
        resource_type="knowledge_document",
        resource_id=str(document.id),
        scope=KnowledgeScope(document.scope),
        scope_id=document.scope_id,
    )


async def authorize_or_not_found(
    access: IdentityAccess,
    actor: ActorContext,
    action: IdentityAction,
    resource: IdentityResource,
    request: Request,
) -> None:
    try:
        await access.authorize(
            actor,
            action,
            resource,
            trace_id=request_trace_id(request),
        )
    except IdentityDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

