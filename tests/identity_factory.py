from dataclasses import dataclass

from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityMembership,
    IdentityRole,
    KnowledgeScopeRef,
    SecurityAuditEvent,
)
from erp_assistant.identity.directory import StaticIdentityDirectory
from erp_assistant.rag.knowledge_corpus_contracts import IdentityCorpusAuthorizer
from erp_assistant.rag.types import KnowledgeScope

_MEMBERSHIPS: dict[str, IdentityMembership] = {}
_MEMBERSHIPS["evaluation-user"] = IdentityMembership(
    tenant_id="tenant-a",
    roles=frozenset({IdentityRole.INTERNAL_EMPLOYEE}),
    knowledge_scopes=frozenset(
        {
            KnowledgeScopeRef(KnowledgeScope.TENANT, "tenant-a"),
            KnowledgeScopeRef(KnowledgeScope.GLOBAL, "GLOBAL"),
        }
    ),
)


def trusted_actor(
    subject_id: str,
    tenant_id: str,
    *,
    roles: frozenset[IdentityRole] | None = None,
    include_global: bool = True,
) -> ActorContext:
    scopes = {KnowledgeScopeRef(KnowledgeScope.TENANT, tenant_id)}
    if include_global:
        scopes.add(KnowledgeScopeRef(KnowledgeScope.GLOBAL, "GLOBAL"))
    membership = IdentityMembership(
        tenant_id=tenant_id,
        roles=(
            roles if roles is not None else frozenset({IdentityRole.INTERNAL_EMPLOYEE})
        ),
        knowledge_scopes=frozenset(scopes),
    )
    _MEMBERSHIPS[subject_id] = membership
    return ActorContext._from_membership(subject_id, membership)


def trusted_identity(
    subject_id: str,
    tenant_id: str,
    *,
    roles: frozenset[IdentityRole],
    include_global: bool = False,
) -> tuple[ActorContext, IdentityAccess]:
    actor = trusted_actor(
        subject_id,
        tenant_id,
        roles=roles,
        include_global=include_global,
    )
    membership = IdentityMembership(
        tenant_id=actor.tenant_id,
        roles=actor.roles,
        knowledge_scopes=actor.knowledge_scopes,
    )
    return actor, IdentityAccess(
        verifier=_UnusedVerifier(),
        directory=StaticIdentityDirectory({subject_id: membership}),
        audit=_NullAudit(),
    )


@dataclass
class _UnusedVerifier:
    async def verify(self, credential: str):
        raise AssertionError(f"unexpected credential: {credential}")


@dataclass
class _NullAudit:
    async def append(self, event: SecurityAuditEvent) -> None:
        del event


@dataclass(frozen=True)
class _FixtureDirectory:
    async def resolve(self, subject_id: str) -> IdentityMembership | None:
        return _MEMBERSHIPS.get(subject_id)


def fixture_identity_access() -> IdentityAccess:
    return IdentityAccess(
        verifier=_UnusedVerifier(),
        directory=_FixtureDirectory(),
        audit=_NullAudit(),
    )


def corpus_authorizer() -> IdentityCorpusAuthorizer:
    reviewer = IdentityMembership(
        tenant_id="tenant-a",
        roles=frozenset({IdentityRole.KNOWLEDGE_ADMIN}),
        knowledge_scopes=frozenset(
            {KnowledgeScopeRef(KnowledgeScope.TENANT, "tenant-a")}
        ),
    )
    service = IdentityMembership(
        tenant_id="*",
        roles=frozenset({IdentityRole.SYSTEM_ADMIN}),
        knowledge_scopes=frozenset(),
        internal_only=True,
    )
    return IdentityCorpusAuthorizer(
        IdentityAccess(
            verifier=_UnusedVerifier(),
            directory=StaticIdentityDirectory(
                {
                    "reviewer-a": reviewer,
                    "knowledge-index-worker": service,
                }
            ),
            audit=_NullAudit(),
        )
    )
