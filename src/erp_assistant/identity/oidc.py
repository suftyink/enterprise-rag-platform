import asyncio
from collections.abc import Mapping
from time import monotonic
from typing import Protocol, cast

import httpx
import jwt
from jwt import exceptions as jwt_exceptions

from erp_assistant.identity.models import IdentityDenied, VerifiedIdentity


class JwksSource(Protocol):
    async def fetch(self) -> dict[str, object]: ...


class HttpJwksSource:
    def __init__(self, url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    async def fetch(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("JWKS document must be an object")
        return cast(dict[str, object], payload)


class JwksCache:
    def __init__(
        self,
        *,
        source: JwksSource,
        ttl_seconds: int,
        max_keys: int,
        min_refresh_interval_seconds: float = 5.0,
    ) -> None:
        if ttl_seconds <= 0 or max_keys <= 0 or min_refresh_interval_seconds < 0:
            raise ValueError("JWKS cache bounds must be positive")
        self._source = source
        self._ttl = ttl_seconds
        self._max_keys = max_keys
        self._min_refresh_interval = min_refresh_interval_seconds
        self._expires_at = 0.0
        self._last_refresh_at = 0.0
        self._keys: dict[str, Mapping[str, object]] = {}
        self._lock = asyncio.Lock()

    async def key(self, kid: str, *, force_refresh: bool = False) -> Mapping[str, object]:
        now = monotonic()
        if not force_refresh and now < self._expires_at and kid in self._keys:
            return self._keys[kid]
        if now - self._last_refresh_at < self._min_refresh_interval:
            key = self._keys.get(kid)
            if key is not None:
                return key
            raise IdentityDenied("UNKNOWN_SIGNING_KEY", "身份验证失败")
        async with self._lock:
            now = monotonic()
            if not force_refresh and now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now - self._last_refresh_at < self._min_refresh_interval:
                key = self._keys.get(kid)
                if key is not None:
                    return key
                raise IdentityDenied("UNKNOWN_SIGNING_KEY", "身份验证失败")
            await self._refresh()
            key = self._keys.get(kid)
            if key is None:
                raise IdentityDenied("UNKNOWN_SIGNING_KEY", "身份验证失败")
            return key

    async def _refresh(self) -> None:
        try:
            document = await self._source.fetch()
            raw_keys = document.get("keys")
            if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= self._max_keys:
                raise ValueError("JWKS key count is invalid")
            parsed: dict[str, Mapping[str, object]] = {}
            for raw_key in raw_keys:
                if not isinstance(raw_key, dict):
                    raise ValueError("JWKS key is invalid")
                kid = raw_key.get("kid")
                if not isinstance(kid, str) or not kid or kid in parsed:
                    raise ValueError("JWKS kid is invalid")
                parsed[kid] = cast(Mapping[str, object], raw_key)
        except IdentityDenied:
            raise
        except Exception as exc:
            raise IdentityDenied("IDENTITY_PROVIDER_UNAVAILABLE", "身份提供方暂时不可用") from exc
        self._keys = parsed
        self._last_refresh_at = monotonic()
        self._expires_at = self._last_refresh_at + self._ttl


class OidcTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: JwksCache,
        algorithms: tuple[str, ...] = ("RS256",),
        subject_claim: str = "sub",
    ) -> None:
        if not issuer or not audience or not subject_claim or not algorithms:
            raise ValueError("OIDC verifier configuration is incomplete")
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._algorithms = algorithms
        self._subject_claim = subject_claim

    async def verify(self, credential: str) -> VerifiedIdentity:
        try:
            header = jwt.get_unverified_header(credential)
            kid = header.get("kid")
            algorithm = header.get("alg")
            if not isinstance(kid, str) or not kid:
                raise IdentityDenied("MISSING_SIGNING_KEY", "身份验证失败")
            if not isinstance(algorithm, str) or algorithm not in self._algorithms:
                raise IdentityDenied("UNSUPPORTED_SIGNING_ALGORITHM", "身份验证失败")
            jwk = await self._jwks.key(kid)
            try:
                claims = self._decode(credential, jwk, algorithm)
            except jwt_exceptions.InvalidSignatureError:
                jwk = await self._jwks.key(kid, force_refresh=True)
                claims = self._decode(credential, jwk, algorithm)
            subject = claims.get(self._subject_claim)
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 128:
                raise IdentityDenied("MISSING_REQUIRED_CLAIM", "身份验证失败")
            return VerifiedIdentity(subject_id=subject.strip())
        except IdentityDenied:
            raise
        except jwt_exceptions.ExpiredSignatureError as exc:
            raise IdentityDenied("TOKEN_EXPIRED", "身份验证失败") from exc
        except jwt_exceptions.InvalidIssuerError as exc:
            raise IdentityDenied("INVALID_ISSUER", "身份验证失败") from exc
        except jwt_exceptions.InvalidAudienceError as exc:
            raise IdentityDenied("INVALID_AUDIENCE", "身份验证失败") from exc
        except jwt_exceptions.MissingRequiredClaimError as exc:
            raise IdentityDenied("MISSING_REQUIRED_CLAIM", "身份验证失败") from exc
        except jwt_exceptions.InvalidSignatureError as exc:
            raise IdentityDenied("INVALID_SIGNATURE", "身份验证失败") from exc
        except jwt_exceptions.PyJWTError as exc:
            raise IdentityDenied("INVALID_TOKEN", "身份验证失败") from exc

    def _decode(
        self,
        credential: str,
        jwk: Mapping[str, object],
        algorithm: str,
    ) -> dict[str, object]:
        key = jwt.PyJWK.from_dict(dict(jwk), algorithm=algorithm).key
        return cast(
            dict[str, object],
            jwt.decode(
                credential,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", self._subject_claim]},
            ),
        )
