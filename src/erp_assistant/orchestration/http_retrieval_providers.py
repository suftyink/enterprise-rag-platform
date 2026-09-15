from contextvars import ContextVar
from math import isfinite
from typing import Any, cast

import httpx

from erp_assistant.orchestration.providers import (
    ProviderBoundary,
    ProviderUsage,
    RankedDocument,
)
from erp_assistant.rag.errors import RagDependencyError


class HttpEmbeddingProvider:
    production_ready = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        deployment: str,
        model: str,
        version: str,
        dimension: int,
        boundary: ProviderBoundary,
        path: str = "/embeddings",
        api_key: str = "",
    ) -> None:
        if not deployment.strip() or not model.strip() or not version.strip() or dimension < 1:
            raise ValueError("Embedding profile identity is invalid")
        self._client = client
        self._path = path
        self._api_key = api_key
        self.deployment = deployment
        self.model = f"{model}:{version}"
        self.dimension = dimension
        self.boundary = boundary
        self._usage: ContextVar[ProviderUsage | None] = ContextVar(
            f"embedding-usage-{id(self)}", default=None
        )

    @property
    def usage(self) -> ProviderUsage:
        return self._usage.get() or ProviderUsage()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._usage.set(ProviderUsage())
        payload = await _post_json(
            self._client,
            self._path,
            {"deployment": self.deployment, "model": self.model, "input": texts},
            self._api_key,
        )
        try:
            data = cast(list[dict[str, Any]], payload["data"])
            rows = sorted(data, key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
            usage = cast(dict[str, Any], payload.get("usage", {}))
            tokens = int(usage.get("prompt_tokens", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise RagDependencyError(
                "PROVIDER_INVALID_RESPONSE", "Embedding Provider 响应无效"
            ) from exc
        if (
            len(vectors) != len(texts)
            or any(len(vector) != self.dimension for vector in vectors)
            or any(not isfinite(value) for vector in vectors for value in vector)
        ):
            raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "Embedding Provider 响应无效")
        self._usage.set(ProviderUsage(requests=1, input_tokens=tokens))
        return vectors


class HttpRerankProvider:
    production_ready = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        deployment: str,
        model: str,
        version: str,
        boundary: ProviderBoundary,
        path: str = "/rerank",
        api_key: str = "",
    ) -> None:
        if not deployment.strip() or not model.strip() or not version.strip():
            raise ValueError("Rerank profile identity is invalid")
        self._client = client
        self._path = path
        self._api_key = api_key
        self.deployment = deployment
        self.model = f"{model}:{version}"
        self.boundary = boundary
        self._usage: ContextVar[ProviderUsage | None] = ContextVar(
            f"rerank-usage-{id(self)}", default=None
        )

    @property
    def usage(self) -> ProviderUsage:
        return self._usage.get() or ProviderUsage()

    async def rerank(self, query: str, documents: list[str]) -> list[RankedDocument]:
        self._usage.set(ProviderUsage())
        payload = await _post_json(
            self._client,
            self._path,
            {
                "deployment": self.deployment,
                "model": self.model,
                "query": query,
                "documents": documents,
            },
            self._api_key,
        )
        try:
            results = cast(list[dict[str, Any]], payload["results"])
            ranked = [
                RankedDocument(index=int(item["index"]), score=float(item["relevance_score"]))
                for item in results
            ]
            usage = cast(dict[str, Any], payload.get("usage", {}))
            tokens = int(usage.get("total_tokens", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise RagDependencyError(
                "PROVIDER_INVALID_RESPONSE", "Rerank Provider 响应无效"
            ) from exc
        if any(not isfinite(item.score) for item in ranked):
            raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "Rerank Provider 响应无效")
        self._usage.set(ProviderUsage(requests=1, input_tokens=tokens))
        return ranked


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, object],
    api_key: str,
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await client.post(path, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise RagDependencyError("PROVIDER_TIMEOUT", "模型 Provider 请求超时") from exc
    except httpx.RequestError as exc:
        raise RagDependencyError("PROVIDER_UNAVAILABLE", "模型 Provider 不可用") from exc
    if response.status_code in {401, 403}:
        raise RagDependencyError("PROVIDER_AUTH_FAILED", "模型 Provider 认证失败")
    if response.status_code == 429:
        raise RagDependencyError("PROVIDER_RATE_LIMITED", "模型 Provider 请求受限")
    if response.is_error:
        raise RagDependencyError("PROVIDER_RESPONSE_FAILED", "模型 Provider 返回错误")
    try:
        value = response.json()
    except ValueError as exc:
        raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "模型 Provider 响应无效") from exc
    if not isinstance(value, dict):
        raise RagDependencyError("PROVIDER_INVALID_RESPONSE", "模型 Provider 响应无效")
    return value
