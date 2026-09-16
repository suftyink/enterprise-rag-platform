import json
from functools import lru_cache

from erp_assistant.core.config import Settings
from erp_assistant.identity.access import IdentityDirectory, IdentityVerifier
from erp_assistant.identity.directory import StaticIdentityDirectory
from erp_assistant.identity.models import (
    IdentityDenied,
    IdentityMembership,
    IdentityRole,
    KnowledgeScopeRef,
    VerifiedIdentity,
)
from erp_assistant.identity.oidc import HttpJwksSource, JwksCache, OidcTokenVerifier
from erp_assistant.rag.types import KnowledgeScope


class DeterministicIdentityVerifier:
    """Local/CI adapter: the credential is an exact configured subject identifier."""

    async def verify(self, credential: str) -> VerifiedIdentity:
        subject_id = credential.strip()
        if not subject_id or len(subject_id) > 128:
            raise IdentityDenied("INVALID_TOKEN", "身份验证失败")
        return VerifiedIdentity(subject_id=subject_id)


def identity_components(settings: Settings) -> tuple[IdentityVerifier, IdentityDirectory]:
    return _identity_components(
        settings.app_env.casefold(),
        settings.identity_provider,
        settings.oidc_issuer,
        settings.oidc_audience,
        settings.oidc_jwks_url,
        settings.oidc_subject_claim,
        settings.oidc_jwks_cache_seconds,
        settings.oidc_jwks_max_keys,
        settings.identity_memberships_json,
    )


@lru_cache
def _identity_components(
    app_env: str,
    provider: str,
    issuer: str,
    audience: str,
    jwks_url: str,
    subject_claim: str,
    cache_seconds: int,
    max_keys: int,
    memberships_json: str,
) -> tuple[IdentityVerifier, IdentityDirectory]:
    directory = StaticIdentityDirectory(_parse_memberships(memberships_json))
    if provider == "test":
        if app_env == "production":
            raise ValueError("生产环境禁止启用测试身份适配器")
        return DeterministicIdentityVerifier(), directory
    if (
        not issuer
        or not audience
        or not jwks_url
        or not subject_claim
        or set(directory.memberships) == {"knowledge-index-worker"}
    ):
        raise ValueError("生产 OIDC 配置或身份映射不完整")
    return (
        OidcTokenVerifier(
            issuer=issuer,
            audience=audience,
            subject_claim=subject_claim,
            jwks=JwksCache(
                source=HttpJwksSource(jwks_url),
                ttl_seconds=cache_seconds,
                max_keys=max_keys,
            ),
        ),
        directory,
    )


def _parse_memberships(payload: str) -> dict[str, IdentityMembership]:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError
        parsed: dict[str, IdentityMembership] = {}
        for subject, value in raw.items():
            if not isinstance(subject, str) or not subject or not isinstance(value, dict):
                raise ValueError
            tenant_id = value.get("tenantId")
            roles = value.get("roles")
            scopes = value.get("knowledgeScopes")
            if (
                not isinstance(tenant_id, str)
                or not tenant_id
                or not isinstance(roles, list)
                or not isinstance(scopes, list)
            ):
                raise ValueError
            parsed[subject] = IdentityMembership(
                tenant_id=tenant_id,
                roles=frozenset(IdentityRole(role) for role in roles),
                knowledge_scopes=frozenset(
                    KnowledgeScopeRef(
                        KnowledgeScope(scope["scope"]),
                        str(scope["scopeId"]),
                    )
                    for scope in scopes
                    if isinstance(scope, dict)
                    and set(scope) == {"scope", "scopeId"}
                    and isinstance(scope["scopeId"], str)
                    and scope["scopeId"]
                ),
            )
            if len(parsed[subject].knowledge_scopes) != len(scopes):
                raise ValueError
        parsed["knowledge-index-worker"] = IdentityMembership(
            tenant_id="*",
            roles=frozenset({IdentityRole.SYSTEM_ADMIN}),
            knowledge_scopes=frozenset(),
            internal_only=True,
        )
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("身份映射配置无效") from exc
