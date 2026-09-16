from dataclasses import dataclass, field
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from erp_assistant.core.config import Settings
from erp_assistant.identity import (
    ActorContext,
    IdentityAccess,
    IdentityAction,
    IdentityDenied,
    IdentityMembership,
    IdentityResource,
    IdentityRole,
    KnowledgeScopeRef,
    SecurityAuditEvent,
    VerifiedIdentity,
)
from erp_assistant.identity.composition import identity_components
from erp_assistant.identity.oidc import JwksCache, OidcTokenVerifier
from erp_assistant.rag.types import KnowledgeScope


@dataclass
class StubVerifier:
    identities: dict[str, VerifiedIdentity]
    errors: dict[str, str] = field(default_factory=dict)

    async def verify(self, credential: str) -> VerifiedIdentity:
        if credential in self.errors:
            raise IdentityDenied(self.errors[credential], "身份验证失败")
        identity = self.identities.get(credential)
        if identity is None:
            raise IdentityDenied("INVALID_TOKEN", "身份验证失败")
        return identity


@dataclass
class MutableDirectory:
    memberships: dict[str, IdentityMembership]

    async def resolve(self, subject_id: str) -> IdentityMembership | None:
        return self.memberships.get(subject_id)


@dataclass
class RecordingAudit:
    events: list[SecurityAuditEvent] = field(default_factory=list)

    async def append(self, event: SecurityAuditEvent) -> None:
        self.events.append(event)


def _membership(*roles: IdentityRole, include_global: bool = False) -> IdentityMembership:
    scopes = {KnowledgeScopeRef(KnowledgeScope.TENANT, "tenant-a")}
    if include_global:
        scopes.add(KnowledgeScopeRef(KnowledgeScope.GLOBAL, "GLOBAL"))
    return IdentityMembership(
        tenant_id="tenant-a",
        roles=frozenset(roles),
        knowledge_scopes=frozenset(scopes),
    )


def _access(
    membership: IdentityMembership | None = None,
    *,
    errors: dict[str, str] | None = None,
) -> tuple[IdentityAccess, MutableDirectory, RecordingAudit]:
    directory = MutableDirectory(
        {"employee-a": membership or _membership(IdentityRole.INTERNAL_EMPLOYEE)}
    )
    audit = RecordingAudit()
    return (
        IdentityAccess(
            verifier=StubVerifier(
                {"valid": VerifiedIdentity(subject_id="employee-a")}, errors or {}
            ),
            directory=directory,
            audit=audit,
        ),
        directory,
        audit,
    )


@pytest.mark.asyncio
async def test_verified_identity_resolves_a_trusted_actor() -> None:
    access, _, audit = _access(_membership(IdentityRole.INTERNAL_EMPLOYEE, include_global=True))
    trace_id = uuid4()

    actor = await access.authenticate("valid", trace_id=trace_id)

    assert actor.subject_id == "employee-a"
    assert actor.tenant_id == "tenant-a"
    assert actor.roles == frozenset({IdentityRole.INTERNAL_EMPLOYEE})
    assert actor.is_trusted is True
    assert actor.can_read(KnowledgeScope.TENANT, "tenant-a") is True
    assert actor.can_read(KnowledgeScope.TENANT, "tenant-b") is False
    assert actor.can_read(KnowledgeScope.GLOBAL, "GLOBAL") is True
    assert audit.events[-1].action is IdentityAction.AUTHENTICATE
    assert audit.events[-1].trace_id == trace_id
    assert audit.events[-1].outcome == "ALLOWED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "TOKEN_EXPIRED",
        "INVALID_SIGNATURE",
        "INVALID_ISSUER",
        "INVALID_AUDIENCE",
        "MISSING_REQUIRED_CLAIM",
    ],
)
async def test_token_failures_are_rejected_and_safely_audited(reason: str) -> None:
    access, _, audit = _access(errors={"bad-secret-token": reason})

    with pytest.raises(IdentityDenied) as raised:
        await access.authenticate("bad-secret-token", trace_id=uuid4())

    assert raised.value.code == reason
    event = audit.events[-1]
    assert event.outcome == "DENIED"
    assert event.reason_code == reason
    assert "bad-secret-token" not in repr(event)


@pytest.mark.asyncio
async def test_employee_and_administrator_permission_matrix_is_scope_bound() -> None:
    tenant_resource = IdentityResource(
        resource_type="knowledge_scope",
        resource_id="tenant-a",
        scope=KnowledgeScope.TENANT,
        scope_id="tenant-a",
    )
    global_resource = IdentityResource(
        resource_type="knowledge_scope",
        resource_id="GLOBAL",
        scope=KnowledgeScope.GLOBAL,
        scope_id="GLOBAL",
    )

    employee_access, _, _ = _access(_membership(IdentityRole.INTERNAL_EMPLOYEE))
    employee = await employee_access.authenticate("valid", trace_id=uuid4())
    await employee_access.authorize(
        employee, IdentityAction.SEARCH, tenant_resource, trace_id=uuid4()
    )
    with pytest.raises(IdentityDenied) as employee_denied:
        await employee_access.authorize(
            employee, IdentityAction.UPLOAD, tenant_resource, trace_id=uuid4()
        )
    assert employee_denied.value.code == "RESOURCE_NOT_FOUND"

    admin_access, _, _ = _access(_membership(IdentityRole.KNOWLEDGE_ADMIN))
    admin = await admin_access.authenticate("valid", trace_id=uuid4())
    await admin_access.authorize(admin, IdentityAction.PUBLISH, tenant_resource, trace_id=uuid4())
    with pytest.raises(IdentityDenied):
        await admin_access.authorize(
            admin, IdentityAction.PUBLISH_GLOBAL, global_resource, trace_id=uuid4()
        )

    system_access, _, _ = _access(_membership(IdentityRole.SYSTEM_ADMIN, include_global=True))
    system_admin = await system_access.authenticate("valid", trace_id=uuid4())
    await system_access.authorize(
        system_admin,
        IdentityAction.PUBLISH_GLOBAL,
        global_resource,
        trace_id=uuid4(),
    )

    no_role_access, _, _ = _access(_membership())
    no_role_actor = await no_role_access.authenticate("valid", trace_id=uuid4())
    with pytest.raises(IdentityDenied):
        await no_role_access.authorize(
            no_role_actor,
            IdentityAction.SEARCH,
            tenant_resource,
            trace_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_revoked_membership_stops_access_and_records_nonleaking_denial() -> None:
    access, directory, audit = _access(_membership(IdentityRole.INTERNAL_EMPLOYEE))
    actor = await access.authenticate("valid", trace_id=uuid4())
    directory.memberships.clear()
    trace_id = uuid4()

    with pytest.raises(IdentityDenied) as raised:
        await access.authorize(
            actor,
            IdentityAction.ANSWER,
            IdentityResource(
                resource_type="knowledge_scope",
                resource_id="tenant-a",
                scope=KnowledgeScope.TENANT,
                scope_id="tenant-a",
            ),
            trace_id=trace_id,
        )

    assert raised.value.code == "RESOURCE_NOT_FOUND"
    assert audit.events[-1].reason_code == "MEMBERSHIP_REVOKED"
    assert audit.events[-1].trace_id == trace_id


def test_unverified_actor_cannot_be_forged_by_request_data() -> None:
    forged = ActorContext(subject_id="employee-a", tenant_id="tenant-b")

    assert forged.is_trusted is False
    assert forged.can_read(KnowledgeScope.TENANT, "tenant-b") is False


@dataclass
class SequenceJwksSource:
    documents: list[dict[str, object] | Exception]
    calls: int = 0

    async def fetch(self) -> dict[str, object]:
        self.calls += 1
        value = self.documents[min(self.calls - 1, len(self.documents) - 1)]
        if isinstance(value, Exception):
            raise value
        return value


def _rsa_key(kid: str) -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private_key, jwk


def _token(private_key: object, kid: str, **overrides: object) -> str:
    claims: dict[str, object] = {
        "sub": "employee-a",
        "iss": "https://identity.example.com",
        "aud": "erp-assistant",
        "exp": 4_102_444_800,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.mark.asyncio
async def test_oidc_verifier_caches_jwks_and_refreshes_for_key_rotation() -> None:
    first_key, first_jwk = _rsa_key("key-1")
    second_key, second_jwk = _rsa_key("key-2")
    source = SequenceJwksSource(
        [
            {"keys": [first_jwk]},
            {"keys": [first_jwk, second_jwk]},
        ]
    )
    verifier = OidcTokenVerifier(
        issuer="https://identity.example.com",
        audience="erp-assistant",
        jwks=JwksCache(
            source=source,
            ttl_seconds=300,
            max_keys=8,
            min_refresh_interval_seconds=0,
        ),
    )

    first = await verifier.verify(_token(first_key, "key-1"))
    again = await verifier.verify(_token(first_key, "key-1"))
    rotated = await verifier.verify(_token(second_key, "key-2"))

    assert first == again == rotated == VerifiedIdentity(subject_id="employee-a")
    assert source.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_overrides", "expected_code"),
    [
        ({"exp": 1}, "TOKEN_EXPIRED"),
        ({"iss": "https://attacker.example.com"}, "INVALID_ISSUER"),
        ({"aud": "other-service"}, "INVALID_AUDIENCE"),
        ({"sub": None}, "MISSING_REQUIRED_CLAIM"),
    ],
)
async def test_oidc_verifier_rejects_invalid_registered_claims(
    claim_overrides: dict[str, object], expected_code: str
) -> None:
    private_key, jwk = _rsa_key("key-1")
    verifier = OidcTokenVerifier(
        issuer="https://identity.example.com",
        audience="erp-assistant",
        jwks=JwksCache(
            source=SequenceJwksSource([{"keys": [jwk]}]),
            ttl_seconds=300,
            max_keys=8,
        ),
    )

    with pytest.raises(IdentityDenied) as raised:
        await verifier.verify(_token(private_key, "key-1", **claim_overrides))

    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_oidc_verifier_fails_closed_when_jwks_is_unavailable() -> None:
    private_key, _ = _rsa_key("key-1")
    verifier = OidcTokenVerifier(
        issuer="https://identity.example.com",
        audience="erp-assistant",
        jwks=JwksCache(
            source=SequenceJwksSource([RuntimeError("provider offline")]),
            ttl_seconds=300,
            max_keys=8,
        ),
    )

    with pytest.raises(IdentityDenied) as raised:
        await verifier.verify(_token(private_key, "key-1"))

    assert raised.value.code == "IDENTITY_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_oidc_verifier_rejects_a_token_signed_by_another_key() -> None:
    configured_key, configured_jwk = _rsa_key("key-1")
    attacker_key, _ = _rsa_key("attacker")
    verifier = OidcTokenVerifier(
        issuer="https://identity.example.com",
        audience="erp-assistant",
        jwks=JwksCache(
            source=SequenceJwksSource([{"keys": [configured_jwk]}]),
            ttl_seconds=300,
            max_keys=8,
        ),
    )
    del configured_key

    with pytest.raises(IdentityDenied) as raised:
        await verifier.verify(_token(attacker_key, "key-1"))

    assert raised.value.code == "INVALID_SIGNATURE"


def test_production_refuses_test_identity_and_incomplete_oidc_configuration() -> None:
    with pytest.raises(ValueError, match="测试身份"):
        identity_components(Settings(app_env="production", identity_provider="test"))
    with pytest.raises(ValueError, match="OIDC"):
        identity_components(
            Settings(
                app_env="production",
                identity_provider="oidc",
                oidc_issuer="https://identity.example.com",
                oidc_audience="erp-assistant",
                oidc_jwks_url="https://identity.example.com/jwks",
            )
        )


@pytest.mark.asyncio
async def test_malformed_jwk_is_rejected_and_audited() -> None:
    private_key, _ = _rsa_key("key-1")
    audit = RecordingAudit()
    verifier = OidcTokenVerifier(
        issuer="https://identity.example.com",
        audience="erp-assistant",
        jwks=JwksCache(
            source=SequenceJwksSource(
                [
                    {
                        "keys": [
                            {
                                "kid": "key-1",
                                "kty": "RSA",
                                "alg": "RS256",
                                "n": "***",
                                "e": "***",
                            }
                        ]
                    }
                ]
            ),
            ttl_seconds=300,
            max_keys=8,
        ),
    )
    access = IdentityAccess(
        verifier=verifier,
        directory=MutableDirectory(
            {"employee-a": _membership(IdentityRole.INTERNAL_EMPLOYEE)}
        ),
        audit=audit,
    )

    with pytest.raises(IdentityDenied) as raised:
        await access.authenticate(_token(private_key, "key-1"), trace_id=uuid4())

    assert raised.value.code in {"INVALID_SIGNATURE", "INVALID_TOKEN"}
    assert audit.events[-1].reason_code == raised.value.code
    assert audit.events[-1].outcome == "DENIED"
