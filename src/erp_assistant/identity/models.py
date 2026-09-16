from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from erp_assistant.rag.types import KnowledgeScope


class IdentityRole(StrEnum):
    INTERNAL_EMPLOYEE = "INTERNAL_EMPLOYEE"
    KNOWLEDGE_ADMIN = "KNOWLEDGE_ADMIN"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"


class IdentityAction(StrEnum):
    AUTHENTICATE = "AUTHENTICATE"
    SEARCH = "SEARCH"
    ANSWER = "ANSWER"
    VIEW_DOCUMENT = "VIEW_DOCUMENT"
    UPLOAD = "UPLOAD"
    RETRY = "RETRY"
    SUBMIT = "SUBMIT"
    INSPECT = "INSPECT"
    PUBLISH = "PUBLISH"
    PUBLISH_GLOBAL = "PUBLISH_GLOBAL"
    REJECT = "REJECT"
    WITHDRAW = "WITHDRAW"
    RESUBMIT = "RESUBMIT"
    ROLLBACK = "ROLLBACK"
    REQUEST_PURGE = "REQUEST_PURGE"
    MARK_CLEANUP_FAILED = "MARK_CLEANUP_FAILED"
    RETRY_CLEANUP = "RETRY_CLEANUP"
    COMPLETE_CLEANUP = "COMPLETE_CLEANUP"


@dataclass(frozen=True)
class KnowledgeScopeRef:
    scope: KnowledgeScope
    scope_id: str


@dataclass(frozen=True)
class IdentityMembership:
    tenant_id: str
    roles: frozenset[IdentityRole]
    knowledge_scopes: frozenset[KnowledgeScopeRef]
    internal_only: bool = False


@dataclass(frozen=True)
class VerifiedIdentity:
    subject_id: str


_TRUST_PROOF = object()


@dataclass(frozen=True)
class ActorContext:
    subject_id: str
    tenant_id: str
    roles: frozenset[IdentityRole] = field(default_factory=frozenset)
    knowledge_scopes: frozenset[KnowledgeScopeRef] = field(default_factory=frozenset)
    _proof: object | None = field(default=None, repr=False, compare=False)

    @property
    def is_trusted(self) -> bool:
        return self._proof is _TRUST_PROOF

    def can_read(self, scope: KnowledgeScope, scope_id: str) -> bool:
        if not self.is_trusted:
            return False
        if scope is KnowledgeScope.TENANT:
            return scope_id == self.tenant_id
        return KnowledgeScopeRef(scope, scope_id) in self.knowledge_scopes

    @classmethod
    def _from_membership(
        cls,
        subject_id: str,
        membership: IdentityMembership,
    ) -> "ActorContext":
        return cls(
            subject_id=subject_id,
            tenant_id=membership.tenant_id,
            roles=membership.roles,
            knowledge_scopes=membership.knowledge_scopes,
            _proof=_TRUST_PROOF,
        )


@dataclass(frozen=True)
class IdentityResource:
    resource_type: str
    resource_id: str
    scope: KnowledgeScope
    scope_id: str


@dataclass(frozen=True)
class SecurityAuditEvent:
    actor_id: str | None
    action: IdentityAction
    resource_type: str
    resource_id: str
    outcome: str
    reason_code: str
    trace_id: UUID
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class IdentityDenied(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
