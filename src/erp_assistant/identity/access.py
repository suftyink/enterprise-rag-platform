from typing import Protocol
from uuid import UUID

from erp_assistant.identity.models import (
    ActorContext,
    IdentityAction,
    IdentityDenied,
    IdentityMembership,
    IdentityResource,
    IdentityRole,
    KnowledgeScopeRef,
    SecurityAuditEvent,
    VerifiedIdentity,
)
from erp_assistant.operations.contracts import NullTelemetry, Telemetry
from erp_assistant.rag.types import KnowledgeScope


class IdentityVerifier(Protocol):
    async def verify(self, credential: str) -> VerifiedIdentity: ...


class IdentityDirectory(Protocol):
    async def resolve(self, subject_id: str) -> IdentityMembership | None: ...


class SecurityAuditSink(Protocol):
    async def append(self, event: SecurityAuditEvent) -> None: ...


_READ_ACTIONS = {
    IdentityAction.SEARCH,
    IdentityAction.ANSWER,
    IdentityAction.VIEW_DOCUMENT,
}
_GOVERNANCE_ACTIONS = {
    IdentityAction.UPLOAD,
    IdentityAction.RETRY,
    IdentityAction.SUBMIT,
    IdentityAction.INSPECT,
    IdentityAction.PUBLISH,
    IdentityAction.REJECT,
    IdentityAction.WITHDRAW,
    IdentityAction.RESUBMIT,
    IdentityAction.ROLLBACK,
    IdentityAction.REQUEST_PURGE,
    IdentityAction.MARK_CLEANUP_FAILED,
    IdentityAction.RETRY_CLEANUP,
    IdentityAction.COMPLETE_CLEANUP,
}


class IdentityAccess:
    """The sole seam for verified identity, resource authorization, and security audit."""

    def __init__(
        self,
        *,
        verifier: IdentityVerifier,
        directory: IdentityDirectory,
        audit: SecurityAuditSink,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._verifier = verifier
        self._directory = directory
        self._audit = audit
        self._telemetry = telemetry or NullTelemetry()

    async def authenticate(self, credential: str, *, trace_id: UUID) -> ActorContext:
        try:
            identity = await self._verifier.verify(credential)
            membership = await self._directory.resolve(identity.subject_id)
            if membership is None or membership.internal_only:
                raise IdentityDenied("MEMBERSHIP_NOT_FOUND", "身份验证失败")
            actor = ActorContext._from_membership(identity.subject_id, membership)
        except IdentityDenied as exc:
            await self._record(
                actor_id=None,
                action=IdentityAction.AUTHENTICATE,
                resource_type="identity",
                resource_id="anonymous",
                outcome="DENIED",
                reason_code=exc.code,
                trace_id=trace_id,
            )
            raise
        await self._record(
            actor_id=actor.subject_id,
            action=IdentityAction.AUTHENTICATE,
            resource_type="identity",
            resource_id=actor.subject_id,
            outcome="ALLOWED",
            reason_code="VERIFIED",
            trace_id=trace_id,
        )
        return actor

    async def reject_authentication(self, reason_code: str, *, trace_id: UUID) -> None:
        await self._record(
            actor_id=None,
            action=IdentityAction.AUTHENTICATE,
            resource_type="identity",
            resource_id="anonymous",
            outcome="DENIED",
            reason_code=reason_code,
            trace_id=trace_id,
        )

    async def authorize(
        self,
        actor: ActorContext,
        action: IdentityAction,
        resource: IdentityResource,
        *,
        trace_id: UUID,
    ) -> None:
        reason = await self._denial_reason(actor, action, resource)
        if reason is not None:
            await self._record(
                actor_id=actor.subject_id if actor.subject_id else None,
                action=action,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                outcome="DENIED",
                reason_code=reason,
                trace_id=trace_id,
            )
            self._telemetry.emit(
                trace_id=trace_id,
                signal="authorization_denial",
                stage="authorization",
                outcome="DENIED",
                attributes={"reason_code": reason},
            )
            raise IdentityDenied("RESOURCE_NOT_FOUND", "资源不存在")
        await self._record(
            actor_id=actor.subject_id,
            action=action,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            outcome="ALLOWED",
            reason_code="AUTHORIZED",
            trace_id=trace_id,
        )

    async def authorize_read_scopes(
        self,
        actor: ActorContext,
        action: IdentityAction,
        *,
        trace_id: UUID,
    ) -> tuple[KnowledgeScopeRef, ...]:
        if action not in _READ_ACTIONS:
            raise ValueError("authorize_read_scopes only accepts read actions")
        scopes = {
            scope
            for scope in actor.knowledge_scopes
            if scope.scope is not KnowledgeScope.TENANT
        }
        scopes.add(KnowledgeScopeRef(KnowledgeScope.TENANT, actor.tenant_id))
        ordered = tuple(sorted(scopes, key=lambda item: (item.scope.value, item.scope_id)))
        for scope in ordered:
            await self.authorize(
                actor,
                action,
                IdentityResource(
                    resource_type="knowledge_scope",
                    resource_id=scope.scope_id,
                    scope=scope.scope,
                    scope_id=scope.scope_id,
                ),
                trace_id=trace_id,
            )
        return ordered

    async def _denial_reason(
        self,
        actor: ActorContext,
        action: IdentityAction,
        resource: IdentityResource,
    ) -> str | None:
        if not actor.is_trusted:
            return "UNTRUSTED_ACTOR"
        current = await self._directory.resolve(actor.subject_id)
        if current is None:
            return "MEMBERSHIP_REVOKED"
        if (
            current.tenant_id != actor.tenant_id
            or current.roles != actor.roles
            or current.knowledge_scopes != actor.knowledge_scopes
        ):
            return "MEMBERSHIP_CHANGED"
        if action in _READ_ACTIONS:
            role_allowed = bool(
                actor.roles
                & {
                    IdentityRole.INTERNAL_EMPLOYEE,
                    IdentityRole.KNOWLEDGE_ADMIN,
                    IdentityRole.SYSTEM_ADMIN,
                }
            )
            scope_allowed = actor.can_read(resource.scope, resource.scope_id)
            return None if role_allowed and scope_allowed else "READ_SCOPE_DENIED"
        if action is IdentityAction.PUBLISH_GLOBAL:
            return (
                None
                if IdentityRole.SYSTEM_ADMIN in actor.roles
                and resource.scope is KnowledgeScope.GLOBAL
                else "GLOBAL_PRIVILEGE_REQUIRED"
            )
        if action is IdentityAction.MANAGE_PILOT:
            return (
                None
                if IdentityRole.SYSTEM_ADMIN in actor.roles
                else "SYSTEM_ADMIN_REQUIRED"
            )
        if action in _GOVERNANCE_ACTIONS:
            return (
                None
                if actor.can_govern(resource.scope, resource.scope_id)
                else "GOVERNANCE_SCOPE_DENIED"
            )
        return "ACTION_DENIED"

    async def _record(
        self,
        *,
        actor_id: str | None,
        action: IdentityAction,
        resource_type: str,
        resource_id: str,
        outcome: str,
        reason_code: str,
        trace_id: UUID,
    ) -> None:
        await self._audit.append(
            SecurityAuditEvent(
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                reason_code=reason_code,
                trace_id=trace_id,
            )
        )


def resource_scope(resource: IdentityResource) -> KnowledgeScopeRef:
    return KnowledgeScopeRef(resource.scope, resource.scope_id)
