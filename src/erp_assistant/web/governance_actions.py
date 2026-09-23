"""Batch the internal governance capabilities for the current actor."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.api.knowledge_schemas import InternalKnowledgeDocumentResponse
from erp_assistant.api.routes.knowledge import document_response
from erp_assistant.identity import ActorContext, IdentityRole
from erp_assistant.models.knowledge import (
    KnowledgeCleanupRequest,
    KnowledgeCorpusHead,
    KnowledgeDocument,
)
from erp_assistant.models.operations import (
    CorpusReleaseDocumentRecord,
    CorpusReleaseRecord,
)
from erp_assistant.operations.release_contracts import ReleaseStatus
from erp_assistant.rag.knowledge_action_policy import governance_action_decision
from erp_assistant.rag.knowledge_corpus_contracts import LifecycleAction
from erp_assistant.rag.knowledge_queries import latest_jobs
from erp_assistant.rag.types import CleanupStatus, KnowledgeScope

VISIBLE_LIFECYCLE_ACTIONS = frozenset(
    {LifecycleAction.PUBLISH, LifecycleAction.WITHDRAW, LifecycleAction.ROLLBACK}
)


async def internal_document_responses(
    session: AsyncSession,
    actor: ActorContext,
    documents: Sequence[KnowledgeDocument],
) -> list[InternalKnowledgeDocumentResponse]:
    if not documents:
        return []
    document_ids = [document.id for document in documents]
    keys = {(item.scope, item.scope_id, item.document_key) for item in documents}
    heads = (
        await session.scalars(
            select(KnowledgeCorpusHead).where(
                or_(
                    *(
                        and_(
                            KnowledgeCorpusHead.scope == scope,
                            KnowledgeCorpusHead.scope_id == scope_id,
                            KnowledgeCorpusHead.document_key == key,
                        )
                        for scope, scope_id, key in keys
                    )
                )
            )
        )
    ).all()
    head_by_key = {(head.scope, head.scope_id, head.document_key): head for head in heads}
    running_cleanups = set(
        await session.scalars(
            select(KnowledgeCleanupRequest.document_id).where(
                KnowledgeCleanupRequest.document_id.in_(document_ids),
                KnowledgeCleanupRequest.status == CleanupStatus.RUNNING.value,
            )
        )
    )
    releases = (
        await session.scalars(
            select(CorpusReleaseRecord)
            .where(
                CorpusReleaseRecord.data_classification.in_(
                    {item.data_classification for item in documents}
                ),
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
            .order_by(
                case(
                    (CorpusReleaseRecord.status == ReleaseStatus.ACTIVE.value, 0),
                    else_=1,
                ),
                CorpusReleaseRecord.activated_at.desc(),
                CorpusReleaseRecord.id.desc(),
            )
        )
    ).all()
    release_by_classification: dict[str, UUID] = {}
    for release in releases:
        release_by_classification.setdefault(release.data_classification, release.id)
    release_ids = list(release_by_classification.values())
    active_members = set(
        (await session.execute(
            select(
                CorpusReleaseDocumentRecord.release_id,
                CorpusReleaseDocumentRecord.document_id,
            ).where(
                CorpusReleaseDocumentRecord.release_id.in_(release_ids),
                CorpusReleaseDocumentRecord.document_id.in_(document_ids),
            )
        )).all()
    ) if release_ids else set()
    jobs_by_document = await latest_jobs(session, document_ids)

    responses: list[InternalKnowledgeDocumentResponse] = []
    for document in documents:
        head = head_by_key.get((document.scope, document.scope_id, document.document_key))
        can_govern = actor.can_govern(KnowledgeScope(document.scope), document.scope_id)
        decision = (
            governance_action_decision(
                document,
                head.published_document_id if head else None,
                cleanup_running=document.id in running_cleanups,
            )
            if can_govern
            else None
        )
        actions = decision.allowed_actions if decision else ()
        if (
            document.scope == KnowledgeScope.GLOBAL.value
            and IdentityRole.SYSTEM_ADMIN not in actor.roles
        ):
            actions = tuple(action for action in actions if action is not LifecycleAction.PUBLISH)
        visible_actions = [
            action.value for action in actions if action in VISIBLE_LIFECYCLE_ACTIONS
        ]
        unavailable_reason = (
            decision.unavailable_reason if decision else "GOVERNANCE_SCOPE_DENIED"
        )
        if (
            not visible_actions
            and unavailable_reason is None
            and document.scope == KnowledgeScope.GLOBAL.value
            and IdentityRole.SYSTEM_ADMIN not in actor.roles
        ):
            unavailable_reason = "GLOBAL_PRIVILEGE_REQUIRED"
        release_id = release_by_classification.get(document.data_classification)
        emergency_release_id = (
            release_id
            if release_id is not None
            and (release_id, document.id) in active_members
            and IdentityRole.SYSTEM_ADMIN in actor.roles
            and LifecycleAction.WITHDRAW in actions
            else None
        )
        response = document_response(document, jobs_by_document.get(document.id))
        responses.append(
            InternalKnowledgeDocumentResponse.model_validate(
                {
                    **response.model_dump(by_alias=True),
                    "allowedActions": visible_actions
                    + (["EMERGENCY_WITHDRAW"] if emergency_release_id else []),
                    "governanceActionReason": unavailable_reason,
                    "emergencyReleaseId": emergency_release_id,
                }
            )
        )
    return responses
